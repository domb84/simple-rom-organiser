# Simple ROM Organiser — architecture & interface contract

Local web app: a Python **standard-library-only** backend (no pip deps — SteamOS has a
read-only root and no pip) serving a plain HTML/JS UI at `http://127.0.0.1:<port>`.
Target platform for now: **SteamOS** (Python 3.11+ is on the host; `7z` and `xdg-open`
are usually present). Code should stay portable (pathlib, no Linux-only calls in core
modules) so Windows/macOS can be added later.

## Layout

```
romorg/
  __init__.py        __version__
  __main__.py        entry point: python -m romorg  (also zipapp entry)
  paths.py           data/config dirs
  tosec.py           find + download + extract TOSEC DAT pack, locate latest DAT
  datfile.py         parse Logiqx XML DAT → DatFile
  scanner.py         walk local dir, hash files/archive members, match to DAT
  organiser.py       plan / apply / undo renames
  m3u.py             group multi-disk sets, plan / write M3U playlists
  server.py          HTTP JSON API + static files + background jobs
  static/            index.html, app.js, style.css  (no build step, no CDN)
tests/               unittest (stdlib only): python -m unittest discover -s tests
packaging/           build_pyz.sh, install.sh, simple-rom-organiser.desktop
```

## paths.py
- `data_dir() -> Path`: `$XDG_DATA_HOME/simple-rom-organiser` (default `~/.local/share/...`), created on demand. On Windows/macOS use sensible equivalents (`%LOCALAPPDATA%`, `~/Library/Application Support`).
- `dats_dir() -> Path`: `data_dir()/dats` (extracted .dat files live here, flat).
- `cache_dir() -> Path`: `data_dir()/cache` (downloaded pack zip, hash cache db).
- `config_path() -> Path`: `data_dir()/config.json` (last dir, last dat).
- `load_config() -> dict`, `save_config(dict)`.

## tosec.py
- `DOWNLOADS_URL = "https://www.tosecdev.org/downloads"`
- Release categories appear on the downloads page as links `/downloads/category/<id>-YYYY-MM-DD`
  (ignore non-date ones like `22-datfiles`, `4-2009-07-12-toseciso-wip`). Latest = max date.
- On the category page the pack link looks like
  `/downloads/category/59-2025-03-13?download=117:tosec-dat-pack-complete-4743-tosec-v2025-03-13`
  (select the href containing `?download=` and `dat-pack-complete`, case-insensitive).
  The response has `Content-Disposition: attachment; filename="TOSEC - DAT Pack - Complete (4743) (TOSEC-v2025-03-13).zip"` (~100 MB).
- Pack zip contains top-level folders `TOSEC/`, `TOSEC-ISO/`, `TOSEC-PIX/`, `CUEs/`, `Scripts/`.
  Extract **only `*.dat` from `TOSEC/`, `TOSEC-ISO/`, `TOSEC-PIX/`** flat into `dats_dir()` (guard against zip-slip;
  basename only). Record release info in `dats_dir()/release.json` `{"release": "2025-03-13", "pack": "<filename>", "downloaded_at": iso}`.
- DAT filenames: `Commodore Amiga - Games - [ADF] (TOSEC-v2025-01-30_CM).dat`. Name = part before ` (TOSEC-v`, version date = `YYYY-MM-DD`.
- API:
  - `@dataclass ReleaseInfo(date: str, category_url: str, download_url: str)`
  - `find_latest_release(fetch=urllib-based) -> ReleaseInfo`
  - `download_pack(info, dest_dir, progress=None, cancel=None) -> Path` — streams with a UA header, progress(bytes_done, bytes_total), resumable skip if a complete file of the same size exists, writes to `.part` then renames.
  - `extract_dats(zip_path, out_dir, progress=None) -> int` (count). Replaces old dats (remove previous `*.dat` first, or extract to temp dir then swap).
  - `update_dats(progress=None, cancel=None) -> dict` — full pipeline; returns `{"release", "count"}`; skips download if `release.json` already has that release (unless `force=True`).
  - `@dataclass DatInfo(name: str, version: str, path: Path)`
  - `list_dats(directory=None) -> list[DatInfo]` — newest version per name, sorted by name.
  - `find_latest_dat(name="Commodore Amiga - Games - [ADF]", directory=None) -> DatInfo | None` (exact name match on the part before ` (TOSEC-v`).
  - `DEFAULT_DAT_NAME = "Commodore Amiga - Games - [ADF]"`

## datfile.py
- `@dataclass(frozen=True) Rom(name: str, size: int, crc: str, md5: str, sha1: str, game: str)` — hashes lowercase hex; crc zero-padded to 8.
- `@dataclass DatFile(name, description, version, roms: list[Rom])` with methods
  `by_sha1() -> dict[str, list[Rom]]`, `by_crc_size() -> dict[tuple[str,int], list[Rom]]` (cached),
  `games() -> dict[str, list[Rom]]`.
- `parse_dat(path) -> DatFile` — use `xml.etree.ElementTree.iterparse` (DAT is 13 MB / 34k entries; must parse in < ~2 s). Ignore DOCTYPE. Tolerate missing md5/sha1 (empty string).
- Note: identical hashes can appear under several names (duplicates in TOSEC) — keep lists.

## scanner.py
- Supported: loose files (any extension, primarily `.adf`), `.zip` (via `zipfile`, CRC from the central directory — no decompress needed for matching by crc+size, but sha1 verification optional), `.7z` / `.rar` via the `7z` binary if on PATH (`7z l -slt -ba` gives Path/Size/CRC per member). If 7z missing, report those archives as `unsupported`.
- Matching: loose files → compute crc32 + sha1 (one pass, 1 MiB chunks), match by sha1 first then crc+size. Archive members → match by crc+size (only what's available cheaply).
- Hash cache: sqlite3 db in `cache_dir()/hashes.sqlite`, key (abs path, size, mtime_ns) → crc, sha1. Must be thread-safe (own connection per scan).
- Recurse subdirectories (option `recursive=True`). Skip hidden files, `*.m3u`, `*.part`, our undo logs.
- API:
  - `@dataclass Entry(path: Path, member: str | None, size: int, crc: str, sha1: str | None)` — `member` set for archive members; `rel` property = path relative to scan root (+ `::member`).
  - `@dataclass Match(entry: Entry, roms: list[Rom])` — roms that match (≥1).
  - `@dataclass ScanResult(root: Path, dat_name: str, matched: list[Match], unmatched: list[Entry], unsupported: list[Path], errors: list[tuple[Path,str]], missing: list[Rom])`
    with `summary() -> dict` → `{"dat_total", "have", "missing", "matched_files", "unmatched_files", "duplicates", "unsupported", "errors", "correctly_named", "to_rename"}`.
    `have` = number of distinct DAT roms (by name) present; `missing` = dat_total - have.
    `correctly_named`: matched loose files whose filename == a matching rom name; archives whose stem == rom's game name and contain only that member.
  - `scan(root, dat: DatFile, recursive=True, progress=None, cancel=None) -> ScanResult` — progress(done_files, total_files, current_name).
  - `to_json(result, limit=None) -> dict` for the API (paths as strings relative to root).

## organiser.py
- Renames **in place**. Loose file → `<rom.name>` (same directory it's in). Archive with exactly one matched member → `<rom.game>.<archive ext>` (TOSEC convention; inner member is NOT rewritten). Archives with multiple members → skip with reason.
- If a file matches several roms (duplicate hashes), prefer: current name already equal to one → no-op; else the rom whose name is closest (difflib ratio) to current name; tie → alphabetical first. Report alternatives.
- Conflicts: target exists and is the same file content (same hash) → mark `duplicate` (do nothing, report); target exists different → `conflict` (skip). Two sources → same target → first wins, rest `conflict`. Case-only rename must work on case-insensitive filesystems (rename via temp name).
- Filenames must be sanitised for the target FS: TOSEC names can contain chars illegal on Windows/FAT/exFAT (`: ? * " < > | \`) — SD cards on Steam Deck are often exFAT. Provide `safe_filename(name)` that replaces illegal chars with `_`; apply always (cheap, portable) and record when changed.
- API:
  - `@dataclass RenameOp(src: Path, dst: Path, status: str, reason: str = "", rom_name: str = "")` status ∈ `rename | ok | conflict | duplicate | skip`.
  - `plan_renames(result: ScanResult) -> list[RenameOp]`
  - `apply_renames(ops, root, progress=None) -> dict` — performs `rename` ops only, writes undo log `root/.romorg-undo-<timestamp>.json` (list of {src,dst}); returns `{"renamed", "failed": [..], "undo_log": path}`. Never overwrites an existing file (re-check at apply time).
  - `undo(log_path) -> dict` — reverse order, skips if dst missing or src exists.
  - `list_undo_logs(root) -> list[Path]`.

## m3u.py
- PUAE m3u rules (https://docs.libretro.com/library/puae/#m3u-and-disk-control): plain text, one disk image path per line, relative paths are relative to the m3u's location; lines starting with `#` are comments; optional `#SAVEDISK:` line adds a blank save disk (do not add by default; option `savedisk=False`); a line can be `path|Label` to label a disk in the disk-control menu. The m3u is loaded as the content and disks appear in RetroArch's disk control in file order.
- TOSEC multi-disk naming: `Title (Year)(Publisher)(Disk 1 of 3)[flags]`. Also seen: `(Disk 1 of 2)(Program)`, `(Disk 2 of 2)(Data)`, `(Disk A of B)` style letters, `(Side A)`. Grouping key = rom name without extension with the `(Disk X of Y)` token removed (keep everything else, so each crack/alt variant `[cr X]`, `[a]` is its own set; per-disk descriptors directly following the disk token like `(Program)`/`(Data)`/`(Save)` should also be removed from the key and kept as the label). Disk index parsed from X (digits, or letters A=1…).
- A set is **complete** when every index 1..Y is present among *matched local files*. Only complete sets are written; incomplete ones are reported with which disks are missing.
- M3U file is written in the **same directory as the disks** (if a set's disks are spread across directories, use the dir of disk 1 and relative paths). Name = `<grouping key>.m3u` (safe_filename applied). Entries reference the **actual current local filenames** (works before or after organising; for archives reference the archive file). Use `\n` line endings, UTF-8 without BOM.
- Existing m3u with identical content → `ok`; different content → overwrite only if it was created by us (first line `#` comment marker `# Generated by simple-rom-organiser`) otherwise `conflict`.
- API:
  - `@dataclass DiskSet(key: str, total: int, disks: dict[int, Match], missing: list[int], directory: Path)` with `complete` property.
  - `group_disk_sets(result: ScanResult) -> list[DiskSet]` (only multi-disk sets, total ≥ 2).
  - `@dataclass M3UOp(path: Path, lines: list[str], status: str, reason="")` status ∈ `write | ok | conflict | incomplete`.
  - `plan_m3us(result, savedisk=False, labels=True) -> list[M3UOp]`
  - `write_m3us(ops) -> dict`.

## server.py
- `ThreadingHTTPServer` bound to **127.0.0.1 only**. Random free port by default (`--port` override). Static from `romorg/static` via `importlib.resources`-style access that also works inside a zipapp (read bytes from the package with `importlib.resources.files("romorg") / "static"`).
- Security: localhost only; reject requests whose `Host` header isn't `127.0.0.1:<port>`/`localhost:<port>` (DNS rebinding); all mutating endpoints POST with JSON body and require header `X-Romorg-Token: <token>` (token injected into index.html at serve time).
- Jobs: one background job at a time (`download`, `scan`, `organise`, `m3u`), state `{id, kind, status: running|done|error|cancelled, progress: {done,total,message}, result, error}`. `GET /api/job` returns current/last job. `POST /api/job/cancel`.
- Endpoints (JSON):
  - `GET /api/status` → `{version, release, dats_count, default_dat, last_dir, last_dat, has_7z, dialog_available}`
  - `POST /api/dats/update {force?}` → starts `download` job
  - `GET /api/dats` → list of `{name, version, file}`; `?q=` filter
  - `GET /api/fs/list?path=` → `{path, parent, dirs: [..], places: [home, /run/media/* mounts, ...]}` for an in-app folder browser (dirs only, hidden excluded by default).
  - `POST /api/fs/pick` → opens native dialog via `kdialog --getexistingdirectory` or `zenity --file-selection --directory` if present (SteamOS desktop mode has kdialog); returns `{path}` or `{cancelled:true}`.
  - `POST /api/scan {path, dat, recursive}` → starts `scan` job; result stored server-side as the "current scan"; job result = summary.
  - `GET /api/scan/results?kind=matched|unmatched|missing|rename|m3u&offset&limit&q` → paged lists from the current scan.
  - `POST /api/organise/plan` → returns plan (counts by status + paged ops), `POST /api/organise/apply` → `organise` job (re-scan automatically after apply), `GET /api/organise/undo-logs`, `POST /api/organise/undo {log}`.
  - `POST /api/m3u/plan {savedisk, labels}` → plan, `POST /api/m3u/apply {savedisk, labels}` → `m3u` job.
- `main(argv)`: args `--port`, `--no-browser`, `--host` (default 127.0.0.1). Opens browser with `webbrowser.open` (falls back to `xdg-open`). Prints URL. Ctrl+C exits cleanly.

## UI (static/)
Single page, no framework, no external resources (offline). Works with mouse/keyboard and at Steam Deck screen size 1280×800; large touch-friendly targets; dark theme default. Sections as steps:
1. **DATs** — current release + "Check for updates / Download" button with progress bar; DAT selector (searchable list of all dats, default Amiga Games ADF).
2. **Folder** — path field + "Browse…" (native dialog if available) + in-app folder browser modal; recursive checkbox.
3. **Scan** — run with progress + cancel; summary cards (have / missing / % complete, matched files, unmatched, to rename, duplicates); tabs for Matched / Missing / Unmatched with search + paging.
4. **Organise** — preview table of renames (from → to, status), filter by status, Apply (confirm dialog), Undo last.
5. **M3U** — preview of complete sets (name, disks) and incomplete sets (missing disks), options (labels, save disk), Write.

## Packaging (SteamOS)
- `packaging/build_pyz.sh` → `dist/simple-rom-organiser.pyz` via `python3 -m zipapp romorg -p "/usr/bin/env python3" -m "romorg.__main__:main"` (copy package into a build dir so it's importable as `romorg`). Must run from the pyz (static files read via importlib.resources).
- `packaging/install.sh` → copies pyz to `~/.local/bin/`, installs `.desktop` to `~/.local/share/applications/` (so it can be "Add a Non-Steam Game" in Steam). `--uninstall` option.

---

# AMENDMENT 1 — platforms, multi-DAT scanning, organise-into-DAT-folders, Kickstart install

**This amendment supersedes the sections above where they conflict.**

## platforms.py (new)
Single place to extend later:
```python
@dataclass(frozen=True)
class Platform:
    name: str                      # "Commodore Amiga"
    dats: tuple[str, ...]          # DAT names (part before " (TOSEC-v"), in PRIORITY order
    m3u_dats: tuple[str, ...]      # DATs whose multi-disk sets get M3Us
    kickstart_dat: str | None      # DAT whose files can be installed as PUAE kickstarts

PLATFORMS = {
  "Commodore Amiga": Platform(
     name="Commodore Amiga",
     dats=("Commodore Amiga - Games - [ADF]",
           "Commodore Amiga - Operating Systems - Workbench",
           "Commodore Amiga - Kickstart-Disks",
           "Commodore Amiga - Firmware"),
     m3u_dats=("Commodore Amiga - Games - [ADF]",
               "Commodore Amiga - Operating Systems - Workbench",
               "Commodore Amiga - Kickstart-Disks"),
     kickstart_dat="Commodore Amiga - Firmware"),
}
def get_platform(name) -> Platform; def list_platforms() -> list[Platform]
def load_platform_dats(platform, directory=None) -> tuple[list[DatFile], list[str]]  # (loaded, missing names)
```
Real DATs present in the 2025-03-13 pack: `Commodore Amiga - Firmware (TOSEC-v2025-01-03_CM).dat` (169: 167 .rom, 2 .adf),
`Commodore Amiga - Kickstart-Disks (TOSEC-v2025-01-03_CM).dat` (43 .adf), `Commodore Amiga - Operating Systems - Workbench (TOSEC-v2023-05-21_CM).dat` (222 .adf).
Note `Commodore Amiga - Operating Systems - AMIX` exists and must NOT be picked up (exact name match).

## datfile.py change
`Rom` gains `dat: str` (DAT name, set by parse_dat from header name / filename part). Keep it hashable.

## scanner.py change
- `scan(root, dats: list[DatFile] | DatFile, ...)` — accepts multiple DATs; combined index. A file matching roms in several DATs: `Match.roms` keeps all, but `Match.primary` (property or field) = matching roms from the highest-priority DAT (order of the `dats` list).
- `ScanResult.dat_names: list[str]` (replaces/extends `dat_name`), `missing` across all dats, `summary()` adds `per_dat: {dat_name: {dat_total, have, missing, matched_files, correctly_placed}}`.
- Scan the selected platform folder **recursively including the DAT subfolders and `_unmatched/`**. Skip hidden files/dirs, `*.m3u`, undo logs, `*.part`.

## organiser.py change — organise into DAT folders
Selected folder = the platform root (e.g. `/run/media/deck/SD/roms/amiga`). Target layout:
```
<root>/
  Commodore Amiga - Firmware/
  Commodore Amiga - Games - [ADF]/
  Commodore Amiga - Kickstart-Disks/
  Commodore Amiga - Operating Systems - Workbench/
  _unmatched/
```
- Folder name = `safe_filename(dat name)` with **no version**. Created on apply as needed.
- Matched loose file → `<root>/<dat folder>/<rom.name>` (flat inside DAT folder). Matched archive (single member) → `<root>/<dat folder>/<rom.game>.<ext>`. DAT chosen = primary match's DAT.
- DAT folder is canonical: any matched file anywhere under root (any depth, incl. `_unmatched/`) not at its canonical path is moved there.
- **Unmatched files** (incl. unsupported archives and multi-member archives with no single match) → `<root>/_unmatched/<path relative to root>` preserving subdirs (to avoid collisions). Files already under `_unmatched/` stay. Unmatched files inside a DAT folder are also moved to `_unmatched/`.
- Never touch: hidden files, our undo logs, `*.m3u` generated by us (they get regenerated); user-made m3u's (not ours) → leave in place.
- RenameOp gains `kind: "rename" | "move"` (or a general "move" status covering both) — keep `status` values: `move` (do it), `ok` (already canonical), `conflict`, `duplicate`, `skip`. Duplicates (same content already at target) → move the redundant copy to `_unmatched/_duplicates/<rel path>`? NO — keep it simple and safe: leave duplicates in place and report them.
- After apply, remove directories under root that **became empty because of our moves** (never root itself, never DAT folders, never pre-existing empty dirs). Record removed dirs in the undo log so undo recreates them.
- Undo log moves remain `.romorg-undo-<timestamp>.json` in root; undo restores exact original paths.
- Case-insensitive FS (exFAT SD cards!) handling for case-only differences remains required.

## m3u.py change
- Only for DATs in `platform.m3u_dats`. Sets grouped per DAT (a set never spans DATs).
- M3U written next to disk 1 (after organising that's the DAT folder). Relative paths.

## kickstart.py (new) — optional "Install Kickstarts for RetroArch/PUAE"
- Source of truth: https://docs.libretro.com/library/puae/ BIOS table (filenames like `kick34005.A500`, `kick40068.A1200`, `kick40063.A600`, `kick33180.A500`, `kick37175.A500`, `kick39106.A1200`, `kick40068.A4000`, `kick40060.CD32`, `kick40060.CD32.ext`, `kick34005.CDTV`, ... with MD5s). Fetch the page and transcribe the full table into a constant `PUAE_BIOS = [(filename, md5, description)]`. Match by MD5 against **local matched files** from the Firmware DAT (compute md5 for those files: scanner gives crc/sha1 only; compute md5 on demand in kickstart.py — or compare via the DAT rom's md5 field, which is exact since the file matched the DAT by sha1/crc+size; prefer the DAT md5). Also allow matching any local scanned file (any DAT) by md5 via the DAT rom's md5.
- Kickstarts in archives: extract member bytes when copying (zip via zipfile; 7z via `7z e -so`).
- `detect_system_dirs() -> list[{"path", "label", "exists"}]` candidates on SteamOS: EmuDeck `~/Emulation/bios`, RetroDECK `~/retrodeck/bios`, Flatpak RetroArch `~/.var/app/org.libretro.RetroArch/config/retroarch/system`, Steam RetroArch `~/.local/share/Steam/steamapps/common/RetroArch/system`, native `~/.config/retroarch/system`. Also EmuDeck on SD: `/run/media/*/Emulation/bios`. Only return existing ones first; user may also type/browse a custom path.
- `plan_kickstarts(result: ScanResult, dest: Path) -> list[KickOp(target: Path, source: Entry|None, status: copy|ok|conflict|missing, reason, description)]` — `missing` = PUAE bios not found locally (informational). `ok` = target exists with same md5. `conflict` = target exists different md5 (don't overwrite).
- `apply_kickstarts(ops) -> dict` — **copies** (never moves) to dest, atomic write via temp file + rename, never overwrites.

## server/UI change
- Replace DAT selection with **platform selection** (dropdown of `list_platforms()`; for each platform show its DATs, version and whether present locally; if any missing, prompt to download/update DATs).
- `GET /api/platforms` → `[{name, dats: [{name, version|null, present}]}]`.
- `POST /api/scan {path, platform}`; results/tabs gain a DAT filter; summary shows per-DAT cards.
- Organise preview shows `from → to` relative paths, grouped by status, plus count of files going to `_unmatched/`.
- New step **Kickstarts (RetroArch)**: choose destination from detected dirs (or browse), preview table, Copy button.
- Config remembers last platform + last folder per platform.

---

# AMENDMENT 2 — AppImage packaging (primary distribution)
- Primary artifact: `dist/Simple_ROM_Organiser-<version>-x86_64.AppImage`, built by `packaging/build_appimage.sh`.
- Self-contained: bundles a relocatable CPython (python-build-standalone) + the `romorg` package; does not rely on host Python.
- Built with appimagetool (static runtime, needs only `fusermount`, present on SteamOS).
- `AppRun` runs `python -m romorg`, which starts the server and opens the browser. Because there's no terminal: UI has a **Quit** button → `POST /api/quit` (token-protected) shuts the server down; logs go to `$XDG_STATE_HOME/simple-rom-organiser/app.log`.
- `packaging/install.sh` installs to `~/Applications` + `.desktop` + icon; add to Steam as a Non-Steam game. Desktop Mode is the primary target.
- The `.pyz` is a secondary artifact.

---

# AMENDMENT 3 — review fixes (additive; supersedes the above where they conflict)
- organiser: `plan_renames(result, missing_dats=(), move_unmatched=True)`. Unmatched files in the
  folder of a missing DAT, frontend files (`gamelist.xml`, `rom.key`, `*.uae`, ...), symlinks, and all
  unmatched files when `move_unmatched=False` → `skip`. New status `delete` (kind `m3u`): our own
  playlists whose disks move (content kept in the undo log). User m3u whose disks move → `skip` note.
  Duplicates blocking another file's canonical name move to `_unmatched/<rel>`; ops whose target
  holder stays become `conflict`. Swap cycles are applied via a temp name.
- `apply_renames(ops, root, progress=None, dat_names=(), cancel=None)` → adds `deleted`, `cancelled`,
  `error`. Undo log v3 = JSON lines written *before* each step: header `{version: 3, root, started}`,
  records `{op: move|mkdir|unmkdir|rmdir|delete|failed|end, src/dst/path (relative to root), i}`.
  `read_undo_log(path)` parses v1/v2/v3 (rebases absolute paths, rejects anything outside the log's
  folder). `undo(log_path, root=None)` → adds `remaining`, `m3us_restored`, `m3us_removed`,
  `rejected`; keeps the log (rewritten) unless every entry was restored.
- m3u: `M3UOp.status` adds `stale` (our playlist not in the plan whose disks are gone, or duplicate of
  a planned one); `write_m3us` removes them and returns `removed`. Helpers `find_m3us(root)`,
  `read_m3u(path) -> (ours, [paths])`, `read_own_m3u_text(path)`, `entry_paths(lines, dir)`.
- scanner: `.romorg-tmp-` leftovers and dangling symlinks are reported in `errors`.
- server: `POST /api/organise/plan|apply {move_unmatched}`; plan adds `warnings`, `missing_dats`,
  `move_unmatched`; apply/undo results add `action` and `rescan_error` (re-scan failures no longer
  fail the job). Scan refuses `/`, home and mount roots. JSON is ASCII-escaped. One instance per data
  dir (`data_dir()/instance.json`; `--new-instance` overrides); only loopback binds.

---

# AMENDMENT 4 — No-Intro consoles (GBA, N64, NES, SNES)

**Binding contract for this phase; supersedes earlier sections where they conflict.**
Everything here is *additive*: existing signatures keep working with their old defaults, the
Amiga/TOSEC behaviour (incl. summary numbers) is unchanged. Python stdlib only.
Builders code against this contract with `getattr(obj, "new_field", default)` where they
consume another builder's new field, so the four parts can be developed in parallel.

## Test data (real files, never copied into the repo)
Scratchpad `nointro/` (libretro, clrmamepro, header `version "2026.08.01"`, no backslash in any file):

| DAT (= header name) | `game (` blocks | distinct game names | **sets** (distinct rom stems) | rom exts |
|---|---|---|---|---|
| Nintendo - Game Boy Advance | 3692 | 3691 | 3692 | .gba |
| Nintendo - Nintendo 64 | 1435 | 1241 | 1257 | 1245 .z64, 178 .v64, 6 .bin, 5 .sm, 1 .cmd |
| Nintendo - Nintendo Entertainment System | 14132 | 7053 | 7070 | 7066 .nes, 7065 .unh, 1 .bin |
| Nintendo - Super Nintendo Entertainment System | 4268 | 4267 | 4268 | 4257 .sfc, 11 .bin |

- Every block has exactly one `rom`. Game keys: `name region serial releaseyear releasemonth releaseday rom`;
  rom keys: `name size crc md5 sha1 serial` (hashes UPPERCASE hex, bare words; names quoted).
- **Alternates share the rom stem**, not just the game name: NES 7062 stems have `.nes`(+16 B iNES) + `.unh`;
  N64 178 stems have `.z64` + `.v64`. Same game name does NOT mean alternates: e.g. N64
  `Aidyn Chronicles - The First Mage (USA) (Beta)` = 5 blocks with different dates; NES
  `Pac-Man (USA) (Tengen)` covers `... (Tengen).nes` and `... (Tengen) (Unl).nes`; GBA
  `Spider-Man 3 (USA)` covers two different dumps. Within one stem no extension repeats; stems are unique per DAT.
- Rom stem != game name for GBA 309, N64 285, NES 6547, SNES 226 roms (rom names carry extra tags such as
  `(Aftermarket) (Unl)`) → **tags and archive names come from the rom stem**.
- No DAT rom has `size % 1024 == 512` (so the SNES header heuristic never hides a real dump).
- Tags seen: regions (`USA, Europe`, `World`, `Asia`...), languages (`En,Fr,De`, `En,Fr,De+En`),
  `Rev 1..5`, `REV-A..H` (SNES), `v1.1`, `v1.02`, `v1.0.1`, `v33.6`, `v4`, `Beta`, `Beta 2`, `Proto 1`,
  `Demo 1`, `Sample`, `Kiosk`, `Promo`, `Debug`, `Unl`, `Aftermarket`, `Pirate`, `Alt`, `Virtual Console`,
  dates `(2000-02-10)`, years `(1996)`, `(PAL)`, `(NTSC)`.
- DAT-o-MATIC GBA (`dom/...20260929-130236.dat`, Logiqx, header `<id>`, version `20260929-130236`):
  98.4 % identical sha1; DOM import is **not** implemented (only used as a Logiqx parse fixture).
- TOSEC `dats/TOSEC/` = the 4 Amiga DATs; Games ADF has 34410 roms, 7698 with ` vX.Y` in the title,
  flags `[cr ..] [h ..] [t ..] [a] [b] [m] [f] [!]`, multi-language `(M4)`, countries `(DE)`, languages `(de-en)`.

## datfile.py (A)
- `Rom` gains, **appended after `dat`** (positional construction keeps working):
  `set_name: str = ""` — the *logical game* ("set"). No-Intro: rom name without its last extension
  (`"X (USA).nes"` → `"X (USA)"`); TOSEC: `""`. Property `tags -> tags.Tags` (lazy import;
  `parse_name(set_name or name, "nointro" if set_name else "tosec")`, lru-cached in tags.py).
- Keys (used by scanner/organiser, define them as module functions here):
  `unit_key(r) -> (r.dat, r.set_name or r.name)` (have/missing/duplicates);
  `archive_stem(r) -> r.set_name or r.game` (archive target name).
- `DatFile` gains `format: str = "logiqx"` (`"logiqx"|"clrmamepro"`), `homepage: str = ""`
  (init fields after `roms`, defaults), method `sets() -> dict[str, list[Rom]]` keyed by
  `set_name or name` (DAT order, cached like `games()`, add `_sets` to the cache attrs that
  `scanner._normalise_dats` resets), property `count_by -> "game" if any rom.set_name else "rom"`.
- `detect_format(path) -> "logiqx" | "clrmamepro"`: first 4 KiB, skip BOM/whitespace; `<` → logiqx;
  first word `clrmamepro`/`game`/`machine` → clrmamepro; else `ValueError("unknown DAT format")`.
- `parse_dat(path, set_names: bool | None = None) -> DatFile` dispatches on `detect_format`.
  `set_names=None` → True for clrmamepro, False for Logiqx; True fills `Rom.set_name` (any format).
- `parse_clrmamepro(path, set_names=True) -> DatFile`: one-regex tokenizer
  `"((?:[^"\\]|\\.)*)"  |  [()]  |  [^\s()"]+`; inside quotes `\"`→`"`, `\\`→`\`, any other
  backslash kept literally. Grammar: `word ( key value | key ( ... ) ... )`. Top-level
  `clrmamepro (` (also `emulator (`) = header: `name description version homepage`; `game (` and
  `machine (` = games (other top-level blocks skipped). Per game: `name` (game name), `rom ( ... )`
  blocks with `name size crc md5 sha1` (others ignored, nested blocks like `disk` skipped); hashes
  through `_hex` (lower, crc zfill 8), size `_int`. Rom `game` = game name, `dat` = header name or
  `dat_name_from_filename`. Unbalanced parens / missing value → `ValueError(f"{path}: line N: ...")`.
  NES (3.3 MB) must parse in < 1.5 s.

## tags.py (A, new) — reusable name-tag parser
```python
@dataclass(frozen=True)
class Tags:
    title: str                    # name w/o extension, tags and version token ("Super Mario World")
    regions: tuple[str, ...]      # No-Intro region names in name order ("USA", "Europe"); TOSEC codes mapped, e.g. "DE"->"Germany"
    languages: tuple[str, ...]    # 2-letter No-Intro style "En","Fr" (TOSEC "de-en" -> ("De","En")); '+' groups flattened, de-duplicated
    languages_implied: bool       # True when no language tag: languages derived from the regions (REGION_LANGUAGE)
    version: str                  # "Rev 1" | "Rev A" | "REV-B" | "v1.1" | "" (TOSEC: " v1.2" token from the title)
    version_key: tuple            # see version_key()
    status: str                   # "" (release) or first status tag verbatim: "Beta 2", "Proto", "Demo 1", "Sample", "Kiosk", "Promo", "Debug", "Tech Demo", "Auto Demo"
    flags: tuple[str, ...]        # every other "(...)" tag verbatim in order (e.g. "Unl","Aftermarket","Virtual Console","2000-02-10","Alt","Disk 1 of 2","AGA","PAL")
    dump_flags: tuple[str, ...]   # every "[...]" verbatim ("b","BIOS","cr Fairlight","a2","!")
    bad: bool                     # [b] / [b...] present
    bios: bool                    # [BIOS] present
    video: tuple[str, ...]        # derived, sorted subset of ("NTSC","PAL"); explicit (PAL)/(NTSC)/(PAL-NTSC) tag wins
    style: str                    # "nointro" | "tosec"

def parse_name(name: str, style: str = "nointro") -> Tags       # functools.lru_cache(maxsize=65536); strips r"\.[A-Za-z0-9]{1,5}$" if present
def version_key(version: str) -> tuple
def supersede_key(t: Tags) -> tuple
def superseded(names: Iterable[str], style: str = "nointro") -> dict[str, str]   # older name -> the newest name that supersedes it
def to_json(t: Tags) -> dict   # {"regions","languages","languages_implied","version","status","flags","dump_flags","bad","bios","video"}
REGIONS: dict[str, str]          # region -> "PAL" | "NTSC" | ""
LANGUAGES: dict[str, str]        # code -> English name
REGION_LANGUAGE: dict[str, str]  # region -> implied language code
TOSEC_COUNTRIES: dict[str, str]  # "DE" -> "Germany" (region name, then REGIONS gives video)
```
- **No-Intro parsing**: split off the extension, then the name is `title (tag) (tag) ... [flag]...`
  (title = text before the first ` (` / ` [`). A paren group is: *region group* if every
  `", "`-separated part is in `REGIONS`; *language group* if every part after splitting on `,` and `+`
  is in `LANGUAGES`; *version* if every part matches `^Rev [0-9A-Z]+$|^REV-[0-9A-Z]+$|^v\d+(\.\d+)*[a-z]?$`
  (several → the highest); *status* if it matches `^(Beta|Proto|Demo|Sample|Kiosk|Promo|Debug|Tech Demo|Auto Demo)( \d+)?$`;
  else a flag. First region group / language group wins, later ones become flags.
- **TOSEC parsing**: `Title[ vX.Y] (date)(publisher)(...)...[flags]`; version token
  `\s(v\d+(\.\d+)*[a-z]?|Rev \d+)$` at the end of the part before the first `(`. Paren 1 = date
  (kept out of the key), paren 2 = publisher (flag, part of the key). Country = `^[A-Z]{2}(-[A-Z]{2})*$`
  via `TOSEC_COUNTRIES`; language = `^[a-z]{2}(-[a-z]{2})*$` (capitalised); `M\d` stays a flag;
  `(PAL)/(NTSC)` set video; devstatus `(beta)/(proto)/(demo...)` → status. `[b...]` → bad.
- `REGIONS` (exact): PAL = Europe, Australia, New Zealand, Germany, France, Spain, Italy, Netherlands,
  Sweden, Denmark, Norway, Finland, Scandinavia, UK, United Kingdom, Ireland, Portugal, Austria,
  Switzerland, Belgium, Greece, Poland, Russia, Croatia, Czech, Hungary, Turkey, South Africa, China,
  Hong Kong, India, Argentina, United Arab Emirates. NTSC = USA, Canada, Japan, Korea, Taiwan, Brazil, Mexico, Peru, Latin America.
  "" = World, Asia, Unknown. Tags: `video` = union over the regions; **World → ("NTSC","PAL")**.
- `TOSEC_COUNTRIES`: AE→United Arab Emirates, AR Argentina, AT Austria, AU Australia, BE Belgium,
  BR Brazil, CA Canada, CH Switzerland, CN China, CZ Czech, DE Germany, DK Denmark, ES Spain, EU Europe,
  FI Finland, FR France, GB United Kingdom, GR Greece, HK Hong Kong, HR Croatia, HU Hungary, IE Ireland,
  IN India, IT Italy, JP Japan, KR Korea, MX Mexico, NL Netherlands, NO Norway, NZ New Zealand,
  PL Poland, PT Portugal, RU Russia, SE Sweden, TR Turkey, TW Taiwan, US USA, ZA South Africa.
- `LANGUAGES` codes: En Ja Fr De Es It Nl Pt Sv No Da Fi Zh Ko Pl Ru El Ca Hu Cs Sk Tr Ar He Hr Is Et Lv
  Lt Sr Sl Ro Bg Uk Id Th Vi Ms Hi Ga Eu Gd Cy.
- `REGION_LANGUAGE`: USA/World/Europe/Australia/New Zealand/UK/United Kingdom/Canada/Ireland/South Africa→En,
  Japan→Ja, Germany/Austria→De, France→Fr, Spain/Mexico/Argentina/Latin America/Peru→Es, Italy→It,
  Netherlands→Nl, Sweden→Sv, Norway→No, Denmark→Da, Finland→Fi, Brazil/Portugal→Pt, Russia→Ru,
  Korea→Ko, China/Taiwan/Hong Kong→Zh, Poland→Pl, Greece→El. Implied only when no language tag.
- `version_key(v)`: `""` → `(0,)`; otherwise `(1, *components)` where `Rev N`/`REV-N` → `(N,)`,
  `Rev A`/`REV-B` → letter index `(1,)`/`(2,)`, `vX.Y.Z` → `(X, Y, Z)` integers (`v1.10` > `v1.9`,
  `v1.02` = (1, 2)), trailing letter adds a component (`v1.2a` → (1, 2, 1)). Ties broken by name.
  Hence: no version < Rev 1 < Rev 2; Rev A < Rev B; v1.1 < v1.2 < v1.10.
- `supersede_key(t)` = `(style, title.casefold(), regions, languages-if-explicit-else-(), status,
  flags, dump_flags without "!")` — **version removed, everything else kept**, so regions never mix,
  Beta/Proto/Demo/Sample (status) are separate groups and never supersede releases, TOSEC `[cr]/[a]/[h]`
  and publisher are part of the key, TOSEC date is not.
- `superseded(names, style)`: group by `supersede_key`; within a group the max `version_key` wins
  and every name with a **strictly lower** key maps to the winner (equal keys never supersede).
  Disk sets: a name with a `Disk N of M` flag is grouped per disk as above, but is superseded only if
  the winning version has all `M` disks among `names` (else no member of that set is superseded).

## nointro.py (A, new) — libretro No-Intro source
```python
BASE_URL = "https://raw.githubusercontent.com/libretro/libretro-database/master/metadat/no-intro/"
USER_AGENT = f"simple-rom-organiser/{__version__}"
NOINTRO_DATS = ("Nintendo - Game Boy Advance", "Nintendo - Nintendo 64",
                "Nintendo - Nintendo Entertainment System", "Nintendo - Super Nintendo Entertainment System")
MANIFEST = "manifest.json"   # {name: {"version", "etag", "sha1", "size", "url", "downloaded_at"}}
class NoIntroError(Exception)
def dat_url(name: str) -> str                      # BASE_URL + urllib.parse.quote(name + ".dat")
def header_version(path) -> str                    # 'version "..."' from the first 4 KiB, "" if absent
def read_manifest(directory=None) -> dict
def list_dats(directory=None) -> list[tosec.DatInfo]   # files named exactly "<name>.dat" (any name), version = manifest or header_version
def find_dat(name, directory=None) -> tosec.DatInfo | None
def download_dat(name, directory=None, progress=None, cancel=None, force=False, opener=None) -> dict
def update_dats(names=None, progress=None, cancel=None, force=False, opener=None) -> dict
```
- Storage: **`paths.nointro_dir()` = `data_dir()/"nointro"`** (new in paths.py, created on demand),
  a sibling of `dats/`, so TOSEC's atomic swap of `dats/` (and `recover_dats_dir`, which only touches
  `.dats-*` siblings) never affects it. Files: `<DAT name>.dat` + `manifest.json` (atomic write).
- `download_dat`: GET with UA; if the file exists and `not force`, send `If-None-Match: <etag>`;
  `304` → `{"name","version","status":"unchanged"}`. Else stream to `<name>.dat.part` (256 KiB chunks,
  `progress(done, total, f"Downloading {name}")`, cancel checked per chunk → delete `.part`, raise
  `tosec.Cancelled`), **validate** with `datfile.parse_dat` (≥1 rom; header name must equal `name`
  else NoIntroError), then `os.replace` and update the manifest (version = `header_version`, etag,
  sha1 of the file). Returns `{"name","version","status":"downloaded"}`. Network/HTTP/validation
  errors raise `NoIntroError` and leave the old file untouched.
- `update_dats(names=None)` (None = `NOINTRO_DATS`): per DAT `download_dat`, collecting errors;
  returns `{"source":"nointro","dats":[{"name","version","status":"downloaded|unchanged|error","error"?}],
  "downloaded","unchanged","failed","count"}` (`count` = DATs present locally afterwards). Raises
  `NoIntroError("Could not reach GitHub (offline?): ...")` only when **every** requested DAT failed
  **and** none of them is present locally. `tosec.Cancelled` propagates. Offline with local DATs:
  scanning keeps working (only local files are ever read for scans).
- Version shown to users = header `version` (e.g. `2026.08.01`). No GitHub API calls (rate limits).

## platforms.py (A)
```python
class DatSource(str, Enum): TOSEC = "tosec"; NOINTRO = "nointro"
LAYOUT_PER_DAT = "per_dat"; LAYOUT_FLAT = "flat"
ALT_SNES_HEADER = "snes_header"; ALT_N64_BYTEORDER = "n64_byteorder"; ALT_NES_HEADER = "nes_header"

@dataclass(frozen=True)
class Platform:
    name: str; dats: tuple[str, ...]; m3u_dats: tuple[str, ...]; kickstart_dat: Optional[str]
    source: str = DatSource.TOSEC          # where its DATs come from
    layout: str = LAYOUT_PER_DAT           # "per_dat" (one folder per DAT) | "flat" (everything in root)
    extensions: tuple[str, ...] = ()       # hints (UI + 7z member eligibility), lower case with dot
    alt_hashes: tuple[str, ...] = ()       # alternate-hash strategies for scanner.scan
    convertible: bool = False              # offers "Convert to No-Intro format"
    folder_hint: str = ""                  # usual folder name (EmuDeck/RetroDECK)
```
| name | dats | ext hints | alt_hashes | convertible | folder_hint |
|---|---|---|---|---|---|
| `Commodore Amiga` (unchanged, per_dat, tosec) | 4 TOSEC DATs | `.adf .zip .7z` | () | False | `amiga` |
| `Nintendo Game Boy Advance` | (`Nintendo - Game Boy Advance`,) | `.gba .zip .7z` | () | False | `gba` |
| `Nintendo 64` | (`Nintendo - Nintendo 64`,) | `.z64 .v64 .n64 .zip .7z` | (`n64_byteorder`,) | True | `n64` |
| `Nintendo Entertainment System` | (`Nintendo - Nintendo Entertainment System`,) | `.nes .unh .zip .7z` | (`nes_header`,) | False | `nes` |
| `Super Nintendo Entertainment System` | (`Nintendo - Super Nintendo Entertainment System`,) | `.sfc .smc .swc .fig .zip .7z` | (`snes_header`,) | True | `snes` |

All four consoles: `source=NOINTRO`, `layout="flat"`, `m3u_dats=()`, `kickstart_dat=None`.
`DEFAULT_PLATFORM` stays `"Commodore Amiga"`; `list_platforms()` sorted by name as now.
- `locate_dats(platform, directory=None, nointro_directory=None) -> dict[str, tosec.DatInfo]` — present
  DATs only (tosec: `tosec.list_dats(directory)`; nointro: `nointro.list_dats(nointro_directory)`), exact names.
- `platform_dat_status(platform, directory=None, nointro_directory=None)` → each row adds `"source"`.
- `load_platform_dats(platform, directory=None, nointro_directory=None)` uses `locate_dats`; No-Intro
  DATs parsed with `parse_dat(path, set_names=True)`; the name-canonicalisation stays.
- `all_dat_names()` unchanged (now also No-Intro names; harmless for flat roots).

## scanner.py (B)
- `scan(root, dats, recursive=True, progress=None, cancel=None, cache_path=None, use_cache=True,
  alt_hashes: Sequence[str] = (), layout: str = "per_dat") -> ScanResult`.
- `Match` gains fields with defaults: `matched_via: str = "raw"` (`"raw"|"headerless"|"byteswapped"`),
  `header: int = 0` (bytes skipped: 512 SNES, 16 NES), `byte_order: str = ""` (`"v64"|"n64"` when
  byteswapped), `alt_crc: str = ""`, `alt_sha1: str = ""` (hash of the normalised content). `Entry`
  stays the **raw** file (crc/sha1/size of the bytes on disk). Files are never modified by scanning.
- Order: raw match first (sha1, then crc+size, as now); only if raw finds nothing try the platform's
  strategies in `alt_hashes` order; the first strategy that matches wins (its roms only).
  - `snes_header`: size % 1024 == 512 and size > 512 → hash bytes `[512:]`, via `headerless`.
  - `nes_header`: first 4 bytes `b"NES\x1a"` and size > 16 → hash `[16:]`, via `headerless`
    (matches the DAT's `.unh` rom of that set; covers dumps whose iNES header differs).
  - `n64_byteorder`: first 4 bytes `80 37 12 40` = z64 (no alt), `37 80 40 12` = v64 → swap each byte
    pair, `40 12 37 80` = n64 → reverse each 4-byte word; via `byteswapped`, `byte_order` set.
    (The DAT's own `.v64` entries match raw — that is a `raw` match.)
  - Loose alt match: sha1 first then crc+size (size = transformed size). Archive member alt match:
    crc+size of the transformed stream.
- Archive members: zip — read the first 4 bytes of the member (cheap) for NES/N64 detection, then
  stream (`zf.open`) the whole member through the transform when eligible. 7z/rar — eligibility
  without reading: SNES size rule; NES member ext `.nes`; N64 member ext `.v64`/`.n64`; then stream
  via `[exe, "e", "-so", "-p", "--", archive, member]` (timeout, errors → `errors`).
- Alt-hash cache: table `alt_hashes(path TEXT, size INTEGER, mtime_ns INTEGER, member TEXT,
  variant TEXT, crc TEXT, sha1 TEXT, PRIMARY KEY (path,size,mtime_ns,member,variant))` in the same
  `hashes.sqlite` (`member=''` for loose files; archive rows keyed by the archive's path/size/mtime;
  `variant` ∈ `snes_header|nes_header|n64_v64|n64_n64`; zip-member alt sha1 may be stored). Same
  degrade-to-no-op rules as `HashCache`; flush deletes stale rows per path.
- **Counting**: `_rom_key` becomes `datfile.unit_key` (`(dat, set_name or name)`). Therefore for No-Intro
  DATs `dat_total/have/missing`, `dat_totals`, `per_dat`, `count_duplicates` and `missing` count **sets**
  (NES: `.nes` or `.unh` present ⇒ the game is "have"; `missing` holds one rom per missing set — the
  first in DAT order); TOSEC (empty set_name) is byte-for-byte unchanged.
- `ScanResult` gains `layout: str = "per_dat"`. `summary()` **adds** (never renames):
  `games_total, games_have, games_missing` (key `(dat, set_name or game)`; equals dat_total/have/missing
  for No-Intro), `count_by` (`"game"` if any loaded DAT has set names, else `"rom"`),
  `matched_via: {"raw": n, "headerless": n, "byteswapped": n}`, `convertible` (matches with via ≠ raw
  whose rom ext is `.sfc` or `.z64`); `per_dat[dat]` adds `games_total, games_have, games_missing, count_by`.
  (tests/test_scanner.py's exact-summary assertion gets the new keys.)
- `game_status(result) -> list[dict]`: one row per set of every loaded DAT in DAT order:
  `{"dat", "name" (set_name or game), "have": bool, "roms": [rom names], "files": [entry.rel...],
  "rom": Rom (first rom of the set)}`; needs the DatFiles → `ScanResult` keeps them in a new
  non-compared field `dats: list[DatFile] = field(default_factory=list, repr=False, compare=False)`.
- `canonical_name(match, members_in_archive=1)` and `is_correctly_placed(match, root,
  members_in_archive=1, layout="per_dat")` delegate to `organiser.target_filename` /
  `organiser.canonical_dir` (lazy import, as `safe_filename` is today); `correctly_placed(result)` uses
  `result.layout`; `is_correctly_named` uses `target_filename` too (an `.smc` matched headerless is
  correctly named as `<set>.smc`).
- `to_json` matched rows add `via, header, byte_order, tags` (`tags.to_json(primary[0].tags)`);
  missing rows add `set_name, tags`; top level adds `layout`.

## organiser.py (C)
- Public helpers (add first — B and convert.py import them):
  `canonical_dir(root, dat_name, layout) -> Path` (`per_dat` → `root/dat_folder_name(dat)`, `flat` → `root`);
  `target_filename(rom, src_name, archive_ext=None, matched_via="raw", byte_order="") -> str`:
  archive → `archive_stem(rom) + archive_ext`; raw → `rom.name`; byteswapped → `stem + {".v64"|".n64"}` by
  `byte_order`; headerless → `stem + original ext` (`stem = rom.set_name or rom name w/o ext`), except
  when the original ext equals the rom's ext (claims to be clean) → `HEADERED_EXT = {".sfc": ".smc",
  ".unh": ".nes"}`; no original ext → rom ext. Always `safe_filename`. **Content is never converted by
  organise, so the extension stays honest** (`.smc`, `.v64`, `.n64`, `.nes`).
  Aliases: `Journal = _Journal`, `rename_no_overwrite = _rename_no_overwrite`, `move_exclusive =
  _move_exclusive`, `make_dirs = _make_dirs`, `temp_name = _temp_name`, `rel_str = _rel_str`,
  `remove_empty_dirs = _remove_empty_dirs`, `protected_dirs = _protected_dirs`, `UndoLogError` (exists).
- Constants: `SUPERSEDED_DIR = "_superseded"`, `CONVERTED_DIR = "_converted_originals"` (both under
  `_unmatched/`), `KEEP_DIRS = {"media", "images", "videos", "manuals", "downloaded_images", "snap", "boxart"}`.
- `plan_renames(result, missing_dats=(), move_unmatched=True, latest_only=False, layout=None)`;
  `layout=None` → `getattr(result, "layout", "per_dat")`.
  - Matched targets: `canonical_dir(root, rom.dat, layout) / target_filename(...)` using the match's
    `matched_via/byte_order` (getattr defaults). Archives as now; choose_rom key for archives uses
    `archive_stem`. `flat`: matched files anywhere under root (subfolders, `_unmatched/`, an old DAT
    folder) move flat into root; unmatched → `_unmatched/<rel>` (same rules, `move_unmatched`, keep files,
    symlinks). The missing-DAT folder rule applies to `per_dat` only. Unmatched files whose first rel
    component (casefold) is in `KEEP_DIRS` → `skip` ("frontend media folder - left in place").
  - Anything under `_unmatched/_converted_originals/` → `ok`, reason "original kept by Convert",
    `unmatched=True`; excluded from latest-only grouping and never moved back.
  - `latest_only=True`: over matched ops (status move/ok/conflict/duplicate, not converted originals),
    per DAT, call `tags.superseded([op.rom_name...], "nointro" if rom.set_name else "tosec")`;
    each superseded op gets `dst = root/_unmatched/_superseded/[<dat folder>/ if per_dat]<its target
    filename>`, `unmatched=True`, `superseded_by = <newer rom name>`, reason
    `"older version - superseded by <name>"`. Done before `_resolve_conflicts`. Only locally present
    files count (the input is local matches).
  - `latest_only=False`: no special case — a matched file in `_unmatched/_superseded/` is a matched file
    off its canonical path, so it moves back (canonical rule). Same for files whose newer version vanished.
- `RenameOp` gains `superseded_by: str = ""`. `plan_counts` adds `to_superseded` (move ops with
  `superseded_by`), `to_unmatched` keeps counting them too.
- **Undo log v3 additions** (format version stays 3; old logs read as before):
  - New record `{"op": "create", "path": rel, "sha1": hex, "size": n, "i": seq}`, journalled *before*
    the created file is moved into place (a following `failed` record cancels it as for moves).
  - `read_undo_log` adds `"created_files": [{"path", "sha1", "size"}]` and `"steps"`: move and create
    records in log order (`{"op":"move","src","dst"}` / `{"op":"create","path","sha1","size"}`);
    `"moves"` stays for compatibility.
  - `undo` walks `steps` in **reverse** (not `moves`): create → if the file exists with the same size and
    sha1, unlink it (`created_removed += 1`); missing → skipped "already gone"; different → skipped
    "changed since it was created" and kept in remaining. Moves as today. Result adds `created_removed`.
  - `_write_log(log_path, root, steps, created, deleted)` rewrites remaining moves *and* creates in order.
- `apply_renames` unchanged (it never creates files).

## convert.py (B, new) — "Convert to No-Intro format"
```python
@dataclass
class ConvertOp:
    src: Path                  # loose file or single-member .zip
    member: Optional[str]      # zip member, else None
    dst: Path                  # clean file: canonical_dir(...)/rom.name  (zip: canonical_dir(...)/<set>.zip holding rom.name)
    original_dst: Path         # root/_unmatched/_converted_originals/<rel of src, leading "_unmatched/" stripped>
    status: str                # convert | conflict | skip
    reason: str = ""
    rom_name: str = ""
    via: str = ""              # headerless | byteswapped
    transform: str = ""        # strip512 | swap16 | swap32
    expected_sha1: str = ""; expected_size: int = 0
def plan_conversions(result, layout=None, latest_only=False) -> list[ConvertOp]
def apply_conversions(ops, root, progress=None, cancel=None) -> dict
def transform_stream(src: BinaryIO, dst: BinaryIO, transform: str) -> tuple[str, str, int]   # (crc, sha1, bytes written)
def convert_counts(ops) -> dict[str, int]
```
- Candidates: matches with `matched_via == "headerless"` whose rom ext is `.sfc` (SNES; `strip512`), and
  `matched_via == "byteswapped"` whose rom ext is `.z64` (`swap16` for v64, `swap32` for n64; swap in
  chunks that are multiples of 4). NES headerless matches are not offered (emulators need the header).
  Files under `_converted_originals/` are ignored. 7z/rar members and multi-member zips → `skip`
  ("extract the archive first" / "archive has N members"). `latest_only=True` → ops whose rom is in
  `tags.superseded(...)` of the local matches → `skip` ("older version").
- Conflicts: `dst` exists (and is not `src`) or claimed by another op, or `original_dst` exists → `conflict`.
- `apply_conversions` uses `organiser.Journal(root)`; per op:
  1. `make_dirs(original_dst.parent, created, journal)`; journal `move src→original_dst`; `rename_no_overwrite`.
  2. Transform from `original_dst` into `temp_name(dst)` in `dst`'s folder (`make_dirs` first); zip:
     write a new zip (`ZIP_DEFLATED`, single member `rom.name`) from the transformed member stream.
     Verify the transformed sha1 == `expected_sha1` and size; mismatch → error.
  3. sha1/size of the file to be created (the zip file itself for zips); journal
     `{"op":"create","path": rel_str(dst, root), "sha1", "size"}`; `move_exclusive(temp, dst)`;
     `journal.useful = True`.
  On any error after step 1: delete the temp, journal + move the original back, record in `failed`.
  Cancel (callable/Event) between ops. Afterwards `remove_empty_dirs` over the sources' parents with
  `protected_dirs` (journal `rmdir`, `unmkdir` exactly as `apply_renames`).
  Returns `{"converted", "failed": [{src, dst, error}], "removed_dirs", "undo_log", "cancelled", "error"}`.
- Undo = the normal `organiser.undo(log)`: deletes the created clean file (sha1-checked), moves the original back.

## server.py + static/* (D)
- `ScanState` gains `latest_only` planning: `rename_plans` keyed by `(move_unmatched, latest_only)`,
  `convert_plans: dict[bool, list]`. Scans call `_call(scanner.scan, ..., alt_hashes=platform.alt_hashes,
  layout=platform.layout)`; `_platform_dats` cache signature from `platforms.locate_dats(platform)`;
  the "no DATs" error names the source ("download the No-Intro DATs first").
- Config: `folders` (existing, per platform name), new `latest_only: {platform: bool}`.
- `GET /api/platforms` → `[{name, source, layout, folder_hint, extensions, convertible, folder: str|null,
  latest_only: bool, dats: [{name, source, version|null, present, file|null, folder, m3u, kickstart}],
  complete, m3u_dats, kickstart_dat}]`.
- `GET /api/status` adds `nointro: {dir, count, dats: [{name, version|null, present}]}`.
- `POST /api/folders {platform, path}` → validates (`_validate_dir`, `_check_scan_root`; `""` forgets) and
  stores `folders[platform]`; returns `{folders}`. Scan still remembers the folder as today.
- `POST /api/dats/update {force?, source?: "tosec"|"nointro"|"all" (default "tosec"), names?: [str]}` →
  `download` job; result: tosec → as today; nointro → `nointro.update_dats(...)` result; all →
  `{"tosec": ..., "nointro": ...}` (a TOSEC failure doesn't stop No-Intro; error only if both fail).
- `POST /api/scan {path, platform}` unchanged shape.
- `GET /api/scan/results`: new `kind=games` (rows from `scanner.game_status`: `{name, dat, have, roms,
  files, tags}`, query `have=1|0`); filters for `matched|missing|games`: `region=`, `language=`,
  `video=PAL|NTSC`, `flag=` (matches `status`, `flags`, or the words `bad`/`bios`); each row adds `tags`
  (`tags.to_json`), matched rows add `via`; response adds `facets: {regions, languages, video, flags:
  {value: count}}` over the kind's rows before the tag filters (after `dat`). Filtering only changes what
  is shown — nothing is organised by filter yet.
- `POST /api/organise/plan|apply {move_unmatched, latest_only}` (latest_only default = config value for the
  platform; a given value is remembered). Plan adds `latest_only`, `to_superseded`, `superseded_dir:
  "_unmatched/_superseded"`; rows add `superseded_by`. `dest` of superseded rows stays `_unmatched`.
- `POST /api/convert/plan {latest_only, status?, q?, offset?, limit?}` → `{items: [{from, to, original_to,
  status, reason, rom_name, via}], total, offset, limit, counts, all, root, available}` (`available =
  platform.convertible`); `POST /api/convert/apply {latest_only}` → job kind `convert`, cancellable between
  files; result = `apply_conversions` result + `action: "convert"`, then re-scan (also after a cancel;
  `summary` / `rescan_error` as organise). Undo via the existing `/api/organise/undo`.
- Kickstart endpoints: platforms with `kickstart_dat=None` → `409 "This platform has no Kickstart DAT"`.
- UI: step 1 becomes **Systems & folders**: one table row per platform — name, source badge (TOSEC /
  No-Intro), DAT version or "missing", folder field (placeholder `/run/media/deck/<SD>/roms/<folder_hint>`),
  Browse… (native) + Folders… (in-app browser), Save (→ `/api/folders`), **Scan** (selects the platform and
  starts the scan). DAT download box with two buttons: "TOSEC pack (Amiga, ~100 MB)" and "No-Intro DATs
  (libretro, 4 small files)" + force checkbox, shared progress bar. Results: for `count_by == "game"`
  cards say Games have/missing; tabs `Games` (have/missing chips) / Matched / Missing / Unmatched /…;
  region/language/video/flag chips from `facets` + small coloured chips per row (regions, languages,
  `via` ≠ raw). Organise: "Latest version only" toggle (per platform, remembered) next to "Move unmatched",
  layout preview text by `layout`. New step **Convert to No-Intro format** (only when `convertible`):
  explanation (originals kept in `_unmatched/_converted_originals/`), preview table, Convert button
  (confirm), shares Undo with Organise. M3U step only when `m3u_dats` non-empty, Kickstart step only when
  `kickstart_dat` set (hidden otherwise).
- README.md / docs/PACKAGING.md: document the consoles, the No-Intro source + storage dir, latest-only,
  convert, and that DATs are fetched from GitHub (libretro-database).

## File ownership (disjoint)
| Builder | Owns (only these) |
|---|---|
| **A – data** | `romorg/datfile.py`, `romorg/tags.py` (new), `romorg/nointro.py` (new), `romorg/platforms.py`, `romorg/paths.py`; `tests/test_datfile.py`, `tests/test_tags.py` (new), `tests/test_nointro.py` (new), `tests/test_platforms.py`, `tests/test_paths.py` |
| **B – scan/convert** | `romorg/scanner.py`, `romorg/convert.py` (new); `tests/test_scanner.py`, `tests/test_convert.py` (new) |
| **C – organise** | `romorg/organiser.py`, `romorg/m3u.py` (only if needed); `tests/test_organiser.py`, `tests/test_m3u.py` (only if needed) |
| **D – server/UI/docs** | `romorg/server.py`, `romorg/static/index.html`, `romorg/static/app.js`, `romorg/static/style.css`, `README.md`, `docs/PACKAGING.md`, `tests/test_server.py` |

Nobody edits: `romorg/tosec.py`, `romorg/kickstart.py`, `romorg/__init__.py`, `romorg/__main__.py`,
`packaging/*`, `docs/ARCHITECTURE.md`, `tests/test_tosec.py`, `tests/test_kickstart.py`,
`tests/test_integration.py`, `tests/test_packaging.py` (integration fixes after merge by the orchestrator).
Order of first edits to unblock others: A lands `Rom.set_name`, `unit_key`, `archive_stem` and the
`tags.py` signatures; C lands `canonical_dir`, `target_filename` and the public aliases. Tests use
synthetic fixtures only (tiny generated clrmamepro/Logiqx text, few-KiB fake ROMs with the real magic
bytes); real DATs only in optional tests skipped when the scratchpad files are absent.

## AMENDMENT 4 — as built (integration notes; supersede the contract above where they differ)
- **tags.py**: `Tags` has an extra `date: str = ""` (TOSEC date, never part of `supersede_key`);
  `flag_kind(flag)` classifies flags (license, alt, date, video, disk, language, serial, distribution,
  event, hardware, other). Variant language codes (`Pt-BR`, `Zh-Hans`) are stored as the base code in
  `languages` and verbatim in `flags`. Also parsed: `V1.1`, `Rev 1.2`, `v2.0-rc2` (< `v2.0`),
  `Possible Proto` / `Debug Version` (status), tags before the region joined to the title, TOSEC
  `v1.3 rev2`, `v1.009 r30`, locale suffix `v1.3GE` (kept as a flag). TOSEC versions in the middle of
  a title (`X v1.2 - Super Enhanced`) stay part of the title. Multi-disk TOSEC sets ignore a disk
  label right after `(Disk N of M)` (e.g. `(Boot)`) when grouping.
- **nointro.py**: `update_dats(..., directory=None)`; downloads send the stored ETag (`304` → unchanged).
  `datfile.detect_format` also accepts `emulator (`.
- **scanner.py**: zip / 7z members matched by an alternate hash are verified by sha1 as well as crc+size
  (the sha1 is computed anyway). A file name also counts as correct when it equals
  `safe_filename(rom name)`. `summary()["convertible"]` excludes files under `_converted_originals/`.
  `duplicates` counts per set, so an NES `.nes` + `.unh` copy of the same game counts as 1 duplicate
  (both files are kept and named; nothing moves).
- **convert.py**: `ConvertOp` adds `expected_crc` (verification when the DAT has no sha1) and `dat`
  (protects the DAT folder from empty-dir cleanup). The kept original keeps its current file name
  (`_unmatched/_converted_originals/<rel>`), so converting before organising keeps e.g. `smoke r1.smc`.
- **organiser.py**: a header-skipped match whose file has no extension gets the headered extension
  (`.smc` / `.nes`), not the clean ROM extension (keeps the extension honest). `KEEP_DIRS` applies to
  flat roots only (an Amiga root's `media/` is still swept into `_unmatched/`). An empty
  `_unmatched/_superseded/` is removed like any other emptied folder.
- **server.py / UI**: organise plan `dest="."` filters the console folder itself. `GET /api/kickstart/dirs`
  still answers 200 for platforms without a Kickstart DAT (plan/apply return 409). In the Games tab facet
  counts are computed after the `have` filter. `GET /api/dats` still lists TOSEC DATs only.
  Fallbacks: `kind=games` builds rows from matched+missing if `scanner.game_status` is absent; missing
  `tags` / `nointro` / `convert` modules → `tags: null` / 501.
- **packaging**: the AppImage import self-check includes `romorg.tags`, `romorg.nointro`, `romorg.convert`
  and `urllib.request`; `smoke_test.sh` checks the `nointro` status block and the five platforms.

## AMENDMENT 5 — review fixes (supersede the contract above where they differ)
- **tags.py**: No-Intro names may start with `[..]` dump flags (`[BIOS] Nintendo Game Boy Advance
  Boot ROM (World) (Rev 1)`): they are removed from the title and added to `dump_flags` (→ `bios`).
  `(MPAL)` (Brazil PAL-M, 60 Hz) sets `video=("NTSC",)` and `flag_kind("MPAL") == "video"`.
  `superseded()`: inside one group, a dotted position (after the first) where any member writes a
  zero-padded part (`v1.02`) compares as a decimal fraction for all members (`v1.02` < `v1.1`,
  `v1.01` < `v1.21` < `v1.3`, `v1.03` < `v1.4`); groups without zero padding keep the integer
  order (`v1.9` < `v1.10`). `version_key()` itself is unchanged.
- **scanner.py**:
  - `summary()`: matches under `_unmatched/_converted_originals/` are excluded from
    `duplicates`, `to_rename` and `matched_via`, and counted as `correctly_named` and
    `correctly_placed` (the organiser leaves them as `ok`); new key `converted_originals`.
    `summary()["convertible"]` = number of `convert.plan_conversions(result)` ops with status
    `convert` (conflicts, 7z / multi-member archives and kept originals are not counted).
  - `is_convertible(match)` is also True for a raw match to a `.v64` / `.n64` DAT rom.
  - SNES copier-header rule: `size % 1024 == 512` **or** `size - 512` is the size of a DAT rom that
    is not a multiple of 1 KiB (`variant_for(..., sizes=)`, `variants_for`, `hash_stream`,
    `hash_file_variants`, `hash_7z_member` take an optional `sizes` container).
  - Archive members whose alternate hashes were computed with no applicable variant are cached as
    variant `"none:<strategies joined by +>"` (crc/sha1 `""`), so they are not decompressed again;
    errors are not cached. `hash_7z_member` stops 7z as soon as the first chunk rules out every variant.
  - `CONVERT_TEMP_MARKER = ".romorg-convert-"`: such files are skipped and reported as
    "partial output of an interrupted convert - the original is kept (run Undo); safe to delete".
- **convert.py**: a raw match to the DAT's `<set>.v64` (`.n64`) whose set also has a `.z64` rom is
  converted to that `.z64` (`swap16` / `swap32`, `via="raw"`, expected hashes of the `.z64`).
  The clean file is written to `<dst name>.romorg-convert-<hex8>` (not `organiser.temp_name`), and
  a `{"op": "tmp", "path": rel}` record is journalled before writing. The journal sha1 of the
  created file comes from the verifying re-read (no extra hash pass).
- **organiser.py**: undo log v3 gains the `tmp` record: `read_undo_log` returns `temp_files`
  (paths whose name contains `CONVERT_TEMP_MARKER`, else `rejected`); `undo` deletes those that
  still exist (result key `temps_removed`; ones it cannot delete stay in the rewritten log).
  `_restore_from_temp` only ever considers `.romorg-tmp-` names. Latest-only groups per
  `(dat, header form)`: a raw match of a `.unh` rom is "headerless", everything else (incl. a
  headered file matched via the `.unh` rom) is "", so a headerless `.unh` never supersedes a
  headered NES copy (and vice versa).
- **server.py**: `POST /api/platforms/options {platform, latest_only}` stores the per-platform choice
  immediately (`400` without `latest_only` / unknown platform) → `{platform, latest_only}`; the UI
  calls it when the checkbox changes. Flag facet: `_tag_values(.., "flag")` also yields dump flags by
  code (`"[cr]"`, `"[a]"`, `"[h]"`, `"[!]"`; `[b]`/`[BIOS]` stay `bad`/`bios`); the `flags` facet
  leaves out flags whose `tags.flag_kind` is `other` (publishers, developers), `disk`, `date` or
  `serial` (filtering by them still works).
- **UI**: header-skipped matches are labelled by header size (512 → "copier header", 16 → "iNES
  header skipped"); the organise confirm dialog shows the console folder as "(console folder)";
  switching systems resets the organise status/destination, convert, M3U and Kickstart filters.
- **packaging**: `install.sh --pyz` rebuilds the zipapp when it is missing or older than any file in
  `romorg/`.

---

# AMENDMENT 6 — auto-updated DATs, duplicates, slot-wise playlists, "Build library"

**Binding contract for this phase; supersedes earlier sections where they conflict.** Python stdlib only.
Everything not mentioned keeps its current behaviour. Signatures below are the *real* current ones plus
the additions; "(new)" marks new names. Five builders work in parallel (see *File ownership* and *Build order*).

## Test data (measured 2026-10-02 on the real DATs, scratchpad `meas6_*.py`)
TOSEC Games [ADF] 2025-01-30 (34410 roms, 19183 with a disk token), Firmware 169, Kickstart-Disks 43,
Workbench 222; libretro No-Intro 2026.08.01 GBA 3692 / N64 1257 / NES 7070 / SNES 4268 sets.
* **No two roms share a sha1 in any DAT** → duplicates only ever come from the user's own copies.
* **Tokens (every status / flag that occurs; counts = roms)**. TOSEC parens: `(demo-playable)` 512,
  `(pre-release)` 206, `(beta)` 195 (+31 Firmware, +13 Workbench), `(demo-rolling)` 123, `(preview)` 24,
  `(demo-slideshow)` 23, `(proto)` 9, `(demo)` 3, `(alpha)` 3 (+2 Workbench). TOSEC brackets (Games): `[cr]` 20368,
  `[h]` 8998, `[t]` 5125, `[a]` 3524+, `[b..]` 3082 (forms: `[b corrupt file]` 786, `[b dump]` 607, `[b corrupt files]` 292,
  `[b doscopy]` 215, `[b crack]` 125, `[b]` 111, `[b2 ..]`, `[b3 ..]` ...), `[m..]` 2830 (`[m bamcopy]`, `[m doscopy]`,
  `[m]`, `[m baddump]` 233, `[m highscore]` 156 ...), `[f]` 1719, `[v ..]` 756 (virus: `[v Saddam 1]`, `[v SystemZ v1.0]` ...),
  `[o]` 157, `[u]` 37, `[unreleased]` 136, `[faked ..]` 19 (+`[fake release]` 6), `[modified tracks]` 9, `[tr ..]` 358, `[p ..]` 97,
  `[beta]` 3, `[technical demo]` 1; notes (kept): `[!]`, `[bootable]`, `[cp ..]`, `[HD]/[FD]/[CD32]/[WB]/[KS*]`, `[data]`, `[docs]`,
  `[compilation]`, `[budget]` 111, `[promo]` 110, `[construction kit]` 102, `[updated*]`, `[release N]`, `[reissue]`,
  `[rebuilt]`, `[inc. ..demo]`, `[fixed]`, `[trainer ..]`, `[developer]`, `[encrypted ..]`, `[unknown hack]` ...
  No-Intro: `Beta` / `Beta N` (GBA 191, N64 99, NES 493, SNES 542 sets), `Proto` / `Proto N` / `Possible Proto` (110/51/435/121),
  `Demo`/`Demo N`/`Tech Demo`/`Auto Demo` (22/5/615/29), `Sample` (1/0/35/20), `Kiosk` (13/0/1/0, also inside
  `(Kiosk, GameCube)`), `Debug` / `Debug Version` (3/6/0/3), `Test Program` (7, DAT-o-MATIC only), kept: `Unl`, `Aftermarket`,
  `Pirate`, `Promo`, `Alt`, `Virtual Console`, `Switch Online`, `Evercade`, `Collection`, `Rev X`, `vX.Y`, `Final Version`, `Hacker`,
  `Illegal Pirated Copy Version`, `NESDev 20xx`, `Byte-Off ..`, `GBA Jam ..`. **The libretro No-Intro DATs contain no `[..]` flag at all**
  (no `[b]`, no `[BIOS]`); the DAT-o-MATIC GBA has 21 names with `[..]` (`[b]` 7 sets, `[BIOS]` 5 sets) - the classifier must still handle them.
  All tokens above are classified by the table below; every other token is *kept* (unclassified list = the "kept" notes above).
* **Game identity (Games [ADF])**: 4555 distinct (title, country, language) keys; 4687 with publisher; **4887 with publisher + edition flags
  (`(AGA)`, `(M3..M10)`, `(PAL)`, `(NTSC)`, `(FW)`, `(OCS-AGA)` ...)**. Without the publisher 124 keys would merge 256 distinct products
  (e.g. 3D Pool Firebird/MicroProse, Apidya Play Byte/Team 17, Arena Defcom/Psygnosis); without edition flags another 200
  AGA/M-language/PAL/NTSC editions (AGA 131, M3 32, M5 26, M4 16, PAL 14, NTSC 13) would merge. Decision: key = title + country + language +
  publisher + edition flags + status tokens (year/version/disk/labels/dump flags are never part of it).
* **ABC pattern**: 165 identity groups have more than one disk-1 version. 28 complete sets take disks from an older version than their
  newest disk 1 (27 of them were reported incomplete by the old per-name grouping); 126 complete sets mix (version, year) titles at all.
  The real ABC rows: `ABC Monday Night Football v1.1 (1991)(Data East)(US)(Disk 1 of 3)` and `...[cr SR]` exist for disk 1 only; disks 2/3 exist
  only as `ABC Monday Night Football (1990)(Data East)(US)(Disk 2 of 3)` / `(Disk 3 of 3)` (+ excluded `(pre-release)` variants).
* **If the user owned EVERY rom** (all rules ON; counts = files, reason priority bad_dump > virus > prototype > pre_release > demo > faked > unreleased > modified > bad_size):
  * Games [ADF] 34410: excluded 7645 (bad_dump 3082, modified 2638, virus 677, demo 630, pre_release 377, bad_size 129, unreleased 81,
    faked 22, prototype 9; non-exclusive hits: modified 2830, virus 756, demo 661, pre_release 431, bad_size 194, unreleased 136, faked 25);
    26765 candidates in 4664 games (223 of the 4887 games are excluded entirely); **kept 8550 files = 4577 games (3056 cracked / 1521 uncracked;
    1759 multi-disk sets = 1759 playlists)**, superseded 17874, incomplete 341 files (87 multi-disk games with no complete set), duplicates 0.
  * Firmware 169: excluded 53 (beta 29, `[o]`/`[u]` 10, `[b]` 9, `[m]` 5), kept 116. Kickstart-Disks 43: excluded 5 (`[b]` 2, `[m]` 3).
    Workbench 222: excluded **125 (56 %: `[m]` 105, beta/alpha 13, `[b]` 5, `[v]` 1, `[o]/[u]` 1)**, 6 of 16 multi-disk games incomplete (7 files).
    All 14 Kickstart roms PUAE needs are `[!]` dumps with no excluded flag (verified by md5).
  * No-Intro (excluded / latest-per-region superseded / kept): GBA 339 / 148 / 3205; N64 157 / 112 / 988; NES 1579 / 415 / 5076; SNES 712 / 308 / 3248.
* **Bad-dump audit**: `tags._is_bad` = `^b\d*(\s.*)?$` on bracket flags; 0 false positives (`[bootable]` does not match; all 11 `[b..]` roms
  in Firmware + Kickstart-Disks are genuine: 9 + 2, 8 of the 9 Firmware ones are `Kickstart ...` roms).
  Gaps: (1) `Tags.bad` loses the flag text, the UI chip says only "bad dump"; (2) `[m baddump]` (233 roms) / `[m highscore]` are *modified*, not bad -
  they must be shown as "Modified [m baddump]"; (3) the flag facet shows the key `bad` unlabelled; (4) `tags.status` keeps only the first status.

## Decisions taken (all follow the brief unless stated)
1. **Duplicates are resolved FIRST** (before exclusions/selection) so the scan summary `duplicates` is profile independent and equals the
   plan's duplicate moves (the brief listed exclusions first; the final layout differs only in which `_unmatched/_*` folder spare copies of an excluded rom land in).
2. Files moved into `_unmatched/_<reason>/` **keep their current filename** (`<rel path>` as the brief says); only kept files are renamed to canonical names.
3. Duplicate keep order adds "raw (exact DAT content) before alternate-hash match" after "already canonical" (N64 `.v64`/SNES `.smc` vs the clean copy).
4. m3u "best-effort sets" (disks with incompatible dump flags) are dropped; such families are reported incomplete.
5. "Complete multi-disk only" applies to every `m3u_dats` DAT (Games, Workbench, Kickstart-Disks); "latest" and "best variant" never to Workbench/Kickstart-Disks/Firmware.
6. Legacy `latest_only` (organise/convert/`/api/platforms/options`) = `profile.latest_only`; `organiser._apply_latest_only` is removed (library does it).
7. `/api/dats/update` stays as an alias of `/api/updates/check`; the TOSEC/No-Intro download buttons and "Force re-download" disappear from the UI.

## tags.py (A)
New / changed:
* `Tags.publisher: str = ""` (last field): TOSEC paren right after the date (also stays in `flags`).
* `RULES = ("bad_dump","virus","bad_size","pre_release","prototype","demo","faked","unreleased","modified")`, `RULE_LABELS: dict[str,str]`
  ("Bad dumps [b]", "Virus-infected [v]", "Over/under dumps [o] [u]", "Pre-release / beta / alpha / preview / debug", "Prototypes", "Demos, samples, kiosk", "Faked [faked]", "Unreleased", "Modified [m]").
* `classify_token(kind: str, text: str) -> Optional[str]` (`kind` `"("`/`"["`) → rule key or None. Paren tokens: split on `", "`, casefold, strip a trailing `\s+\d+`;
  `beta alpha preview pre-release prerelease debug "debug version" "test program"` → pre_release; `proto "possible proto"` → prototype;
  `demo demo-* "tech demo" "auto demo" sample kiosk` → demo; `unreleased` → unreleased. Bracket tokens (case-sensitive codes): `^b\d*(\s.*)?$` bad_dump;
  `^v\d*(\s.*)?$` virus; `^[ou]\d*(\s.*)?$` bad_size; `^m\d*(\s.*)?$|^modified\b` modified; `^fake(d)?\b` faked; `^unreleased\b` unreleased;
  case-insens. `^(beta|alpha|preview|pre-release)\b` pre_release, `^proto(type)?\b` prototype, `^(technical )?demo\b` demo (so `[inc. StarRay demo]`, `[U34]`, `[unknown hack]`, `[bootable]` are NOT matched).
  NOT classified (kept): `[cr] [h] [t] [a] [f] [tr] [p] [!] [BIOS]`, `(Unl) (Aftermarket) (Pirate) (Promo) (Alt) (AGA)`, publishers, everything else.
* `exclusion_info(t: Tags) -> list[tuple[str, str]]` = `(rule, exact text incl. delimiters)` over `[t.status] + t.flags` (parens; the `t.publisher` group is skipped) and `t.dump_flags` (brackets), name order, de-duplicated;
  `exclusion_rules(t) -> frozenset[str]`; `bad_flags(t) -> list[str]` (exact `"[b corrupt file]"` texts); public `is_bad(flag)` (alias `_is_bad` kept).
* `is_cracked(t) -> bool`: any dump flag `^cr\d*(\s.*)?$`. `modification_count(t) -> int`: dump flags `^(t|h|tr|f|a)\d*(\s.*)?$`.
* `identity_key(t: Tags) -> tuple` = `(style, title.casefold(), regions, explicit languages (() if implied), publisher.casefold(), edition, statuses)`;
  `edition` = `t.flags` before the disk flag (everything after it is a disk label), minus the publisher and `flag_kind == "date"` flags; `statuses` = tuple of paren/bracket texts of rules pre_release/prototype/demo/unreleased.
  Version, date, disk token, labels and variant dump flags are NOT in it. (No-Intro: `(style, title.casefold(), regions)`.)
* `group_version_keys(names: Iterable[str], style: str) -> dict[str, tuple]` (public `_group_keys`, zero-padded fractions), `date_key(date: str) -> str` (`x`/`?` → `0`).
* `to_json(t)` adds `"bad_flags": [...]`, `"excluded_by": [{"rule", "text"}]`. `superseded()` unchanged (No-Intro latest-per-region and convert still use it).

## library.py (A, new)
```python
RULES = tags.RULES
@dataclass(frozen=True)
class LibraryProfile:
    exclude: frozenset[str] = frozenset(RULES)   # enabled exclusion rules
    latest_only: bool = True
    best_variant: bool = True                    # Amiga Games only (implies latest for that DAT)
    complete_only: bool = True                   # multi-disk sets
    def to_dict(self) -> dict    # {"exclude": sorted list, "latest_only", "best_variant", "complete_only"}
    @classmethod from_dict(cls, d, defaults)     # unknown keys / rules ignored, missing -> defaults
    @classmethod latest_only_profile(cls)        # exclude=∅, best_variant=False, complete_only=False, latest_only=True (legacy flag)
def default_profile(platform) -> LibraryProfile  # best_variant False if not platform.best_variant_dats; complete_only False if not platform.m3u_dats; latest_only False if not platform.latest_dats
def load_profile(cfg: dict, platform) -> LibraryProfile    # cfg["library"][platform.name]; legacy cfg["latest_only"][name]=True sets latest_only
def store_profile(cfg: dict, platform, profile) -> None    # writes cfg["library"][platform.name]
@dataclass
class Item:        # one local FILE that survived duplicate removal (built by organiser.plan_renames)
    key: int; dat: str; rom: Rom; style: str  # "tosec"|"nointro"; rom = the rom chosen by organiser.choose_rom
    path: Path; member: Optional[str]; form: str = ""   # form: organiser._header_form
KEEP, EXCLUDED, SUPERSEDED, INCOMPLETE = "keep","excluded","superseded","incomplete"
@dataclass
class Decision:
    key: int; action: str; codes: tuple[str,...] = (); detail: str = ""      # excluded: rule keys + exact flag texts ("[b corrupt file]")
    superseded_by: str = ""; set_id: Optional[int] = None; missing: tuple[int,...] = (); reason: str = ""   # human text
@dataclass
class ChosenSet:    # a kept multi-disk set -> one playlist
    id: int; dat: str; name: str; total: int; slots: dict[int,int]; labels: dict[int,str]; flags: tuple[str,...]; cracked: bool
@dataclass
class IncompleteSet: dat: str; name: str; total: int; present: dict[int,int]; missing: tuple[int,...]
@dataclass
class Selection: decisions: dict[int, Decision]; sets: list[ChosenSet]; incomplete: list[IncompleteSet]
def select(items: Sequence[Item], profile: LibraryProfile, platform) -> Selection
def exclusion_of(rom_name: str, style: str, profile) -> tuple[tuple[str,...], str]   # (rule keys, exact texts)
```
`select` (pure, deterministic, independent of file location → idempotent):
1. **Exclude**: `tags.exclusion_info(item.rom.tags)` ∩ `profile.exclude`, for every DAT of the platform → `EXCLUDED` (reason text "excluded: bad dump [b corrupt file]").
2. Remaining items per DAT. DATs in `platform.latest_dats`/`best_variant_dats`/`m3u_dats` get the rules below; others keep everything.
3. **TOSEC DATs**: build per file a `Cand(name=item.rom.name)`; group by `(dat, tags.identity_key)`. Single-disk (no disk token) = a complete set of 1.
   Multi-disk: `m3u.resolve_slots(cands, tags.group_version_keys(...))` (slot-wise, see m3u.py) per identity group.
4. **Completeness** (`complete_only` and DAT in `m3u_dats`): groups with no complete set → all their candidates `INCOMPLETE` (`missing` = missing slot numbers of the best partial set, `IncompleteSet` listed). Without `complete_only` incomplete groups are kept untouched.
5. **Games DAT with `best_variant`**: among the group's complete sets keep exactly ONE, by sort key `(not cracked, newer version first, fewer modification flags, anchor rom name casefold)`;
   cracked = any disk `tags.is_cracked`; version = `group_version_keys` of disk 1 then `date_key`; mods = Σ `modification_count`. Items of the group not in the kept set → `SUPERSEDED` (`superseded_by` = playlist/rom name). Kept sets with total ≥ 2 → `ChosenSet`.
6. **Games DAT with `latest_only` only** (best_variant off): keep, per `(identity, set flag signature = non-neutral flags)`, the newest complete set; older → SUPERSEDED. Workbench/Kickstart-Disks/Firmware: every complete set is kept (and gets a `ChosenSet`), disks in no complete set follow rule 4.
7. **No-Intro** (`latest_dats`): per `(dat, item.form)` `tags.superseded(names, "nointro")` → SUPERSEDED (region/lang/flags are part of its key, so latest is per region).
8. Kept multi-disk sets of m3u DATs always yield a `ChosenSet` (also when best_variant is off).
Tests (A): the ABC regression with the exact names above (own v1.1 disk 1 `[cr SR]`, `(1990)` disks 2+3, plus `(pre-release)` disks and a `[b ..]` disk 1: chosen set = v1.1 + (1990) 2/3, name `ABC Monday Night Football v1.1 (1991)(Data East)(US)[cr SR]`), ranking order (cracked v1.0 beats uncracked v1.1; newest cracked beats older cracked; crack-only game kept), publisher/edition separation, every rule on/off, No-Intro per-region latest, idempotence of `select` on a permuted item order, token table (every token of *Test data*).

## m3u.py (B)
Slot-wise resolution replaces the per-name grouping (`DiskName`, `parse_disk_name`, `is_neutral_flag`, `generalises`, `_compat`, `M3U_MARKER`, `find_m3us`, `read_m3u`, `read_own_m3u_text`, `entry_paths`, `write_m3us` unchanged).
```python
@dataclass(frozen=True)
class DiskCand: ref: Any; name: str          # ref = caller handle (library Item.key / scanner Match); name = TOSEC rom name
@dataclass
class SlotSet:
    identity: tuple; total: int; anchor: Optional[DiskCand]; slots: dict[int, DiskCand]; missing: list[int]
    flags: tuple[str,...]; labels: dict[int,str]; name: str; note: str = ""
    complete (property)
def resolve_slots(cands: Sequence[DiskCand], vkeys: Optional[Mapping[str, tuple]] = None) -> list[SlotSet]
```
`cands` = the multi-disk candidates of ONE `tags.identity_key` group (any totals; sets are built per total). Rules:
* **Anchors** = every disk-1 candidate. For an anchor `A` with flag set `F`: slot `i` = best candidate `c` of disk `i` with `_compat(c.flags, F) is not None` (a bare/unflagged disk fits any `[cr X]` anchor; a different crack, `[t ..]`, `[h ..]`, `[a]` not in `F` never fits)
  **and `vkeys[c] <= vkeys[A]`**; best = newest `(vkey, date_key)`, then exact flags over generalised, more shared flags, shorter then alphabetical name. Callers never pass excluded/bad variants.
* Sets selecting the same refs are merged; incomplete sets whose refs are a subset of another set are dropped; no disk-1 candidate → one pseudo-set (`anchor=None`, anchored on the newest candidate, `missing` includes 1). No best-effort sets.
* `name` = anchor name: stem with the disk token + labels removed, flags kept, plus neutral flags shared by every chosen disk dropped as before (`ABC Monday Night Football v1.1 (1991)(Data East)(US)[cr SR]`). Labels per disk as today.
Existing API on top: `group_disk_sets(result, dats=None) -> list[DiskSet]` and `plan_m3us(result, savedisk=False, labels=True, dats=None, exclude_rules=None)` now group by `(dat, tags.identity_key)` and call `resolve_slots`
(this fixes the ABC bug in the stand-alone M3U step; `DiskSet.key` = `SlotSet.name`); `exclude_rules` (default all `tags.RULES`; server passes `profile.exclude`) removes excluded roms from eligibility. `(N disks)` / case-insensitive collision naming kept.
Library playlists (new):
```python
@dataclass
class PlaylistDisk: slot: int; path: Path; member: Optional[str]; label: str    # path = FINAL location of the disk
@dataclass
class PlaylistSpec: name: str; dat: str; total: int; disks: list[PlaylistDisk]
def plan_playlists(specs, root: Path, savedisk=False, labels=True, member_counts: Mapping[str,int] = {}) -> list[M3UOp]   # write | ok | conflict
def plan_stale(root: Path, planned: list[M3UOp], moving: Collection[str] = ()) -> list[M3UOp]               # status "stale" (own playlists: disks gone / replaced / in `moving`)
def content_of(lines) -> str;  def write_playlist(path: Path, text: str, replace_own: bool = False) -> tuple[str, int]   # (sha1, size); raises FileExistsError when not allowed
```
The playlist is written in the directory of disk 1's final path; lines relative to it; name collisions as today. Tests (B): the ABC names (3 disks, `|Disk N` labels, exact file name), version-bleed (v1.1 disk 1 + v1.0 disk 2 allowed, newer disk 2 not), different cracks never mixed, excluded disk never borrowed, merged/dropped sets, stale handling, no best-effort.

## scanner.py (C)
* `duplicate_groups(result: ScanResult, layout: Optional[str] = None) -> list[tuple[Path, list[Path]]]` (new; single source of truth, `(keeper path, loser paths)`, absolute). Unit = a *file* the organiser treats as one game (loose matched file, or archive whose members all match one game; originals under `_unmatched/_converted_originals/` excluded).
  Group key = `(dat, frozenset(unit_key(r) for primary roms of the file), organiser._header_form(..))` → same content as loose file vs single-member archive vs inside a multi-rom archive groups together; NES `.nes` vs `.unh` (form differs) do not.
  Keeper = the minimum of `(not already at canonical path+name, lives under _unmatched/, not a raw match, is an archive, number of path parts, len(path), path.casefold())` (so: canonical, then outside `_unmatched/`, then exact content, then loose over archive, then shortest path, then alphabetical).
* `summary()["duplicates"]` = number of losers **not yet under `_unmatched/_duplicates/`** (= duplicate moves the plan makes); new keys `duplicates_set_aside` (losers already there), `duplicate_groups`, `bad_dump_files` (matched files whose primary rom `tags.bad`). `count_duplicates(matches)` is kept as a thin wrapper (old semantics unused).
* `to_json` rows pick up `bad_flags` / `excluded_by` through `tags.to_json`.
Tests (C): the old NES `.nes`+`.unh` "1 duplicate" test changes to 0; loose + zip of one rom = 1; keeper order; `summary["duplicates"] == plan_counts["to_duplicates"]` on mixed fixtures (loose/zip/subfolders/already set aside).

## organiser.py (C)
* New constants `EXCLUDED_DIR="_excluded"`, `DUPLICATES_DIR="_duplicates"`, `INCOMPLETE_DIR="_incomplete"` (+ `SUPERSEDED_DIR`, `CONVERTED_DIR`), `REASON_DIRS`; `reason_destination(src, root, reason_dir) -> Path` = `root/_unmatched/<reason_dir>/<rel_core>` where `rel_core` = path relative to root with a leading `_unmatched/` and, if present, `<reason dir>/` removed; the file name is unchanged.
* `RenameOp` adds `code: str = ""` (`"" | "excluded" | "superseded" | "incomplete" | "duplicate"`), `keeper: str = ""` (rel path of the kept file, duplicates), `missing: tuple[int,...] = ()`, `flags_text: str = ""` (exact flags, excluded). `unmatched=True` for all of them; `superseded_by` kept.
* `plan_renames(result, missing_dats=(), move_unmatched=True, latest_only=False, layout=None, profile: Optional[LibraryProfile] = None)`; order: build canonical ops (unchanged) → symlink skip → **duplicates** (always, via `scanner.duplicate_groups`; losers → `reason_destination(.., DUPLICATES_DIR)`, status `move`, code `duplicate`; losers already there are `ok`) →
  if `profile` (or legacy `latest_only` → `LibraryProfile.latest_only_profile()`): build `library.Item`s for surviving matched files, `library.select`, apply `Decision`s (`excluded/superseded/incomplete` → reason folder, `ok` when already there) → `_resolve_conflicts` → `_plan_m3u_ops`.
  A file under `_unmatched/_<reason>/` whose decision is `keep` keeps its canonical op (moves back). The old `duplicate` status remains only for "identical file already at target" outside the scan. `_apply_latest_only` is removed.
* `LibraryPlan` (new dataclass): `ops: list[RenameOp]` (file moves, `delete` ops for stale own playlists), `playlists: list[m3u.M3UOp]` (write/ok/conflict), `selection: library.Selection`, `profile`; `counts() -> dict`.
  `plan_library(result, profile, missing_dats=(), move_unmatched=True, layout=None, savedisk=False, labels=True) -> LibraryPlan`: `plan_renames(profile=profile)`, then `m3u.plan_playlists` from `selection.sets` using each kept disk's FINAL path (`op.dst` if the op moves/renames/ok, else `src`), then stale own playlists (`m3u.plan_stale` → `delete` ops); a playlist path that is both deleted and rewritten is a single `write` ("updates playlist previously generated").
* `plan_counts(ops)` adds `to_excluded`, `to_superseded`, `to_duplicates`, `to_incomplete` (`to_unmatched` stays = every move into `_unmatched/`). `reason_counts(plan) -> dict` = `{kept, renamed, moved, excluded, superseded, incomplete, duplicates, unmatched, conflict, skip, playlists_write, playlists_ok, playlists_remove, playlists_conflict}`.
* `apply_renames(ops, root, progress=None, dat_names=(), cancel=None, playlists: Sequence[M3UOp] = ())`: after moves and `delete` ops, writes the `write` playlists in the SAME journal: if an own file exists → `delete` record (content) first; then `{"op":"create","path","sha1","size"}` **before** `m3u.write_playlist`. A playlist whose disk no longer exists at its planned path is skipped (reported in `failed`). Result adds `playlists_written`. `undo()` is unchanged (create → removed if size+sha1 unchanged, deleted playlists restored) = ONE undo.
* **Idempotence**: re-scan + `plan_library` on an applied library gives only `ok`/`skip` ops, no playlist `write`/`stale`; selection never depends on a file's location except the duplicate keeper rule (canonical first). Emptied `_unmatched/_<reason>/` folders are removed like other emptied folders.
Tests (C): reasons → folder mapping, move-back after switching a rule off, duplicate flavours, idempotent second plan (empty), undo removes created playlists / restores deleted ones, layout flat + per_dat, case-insensitive FS, symlinks, `integration` end-to-end ABC library (tests/test_integration.py).

## tosec.py, nointro.py, platforms.py, paths.py (E)
* `tosec.py`: `installed_release(directory=None) -> Optional[str]` (release.json `release` only when ≥1 `.dat` exists); `check_latest(fetch=default_fetch, timeout=None) -> ReleaseInfo` (= `find_latest_release`, `default_fetch(url, timeout=TIMEOUT)`);
  `update_dats(..., info: Optional[ReleaseInfo] = None)` skips discovery when given and downloads only when `info.date != installed_release()` (or `force`); `extract_dats(..., release: Optional[dict] = None)` writes `release.json` **into the temp dir before the swap** so DATs + release.json swap atomically. Cancel keeps the resumable `.part`, removes the temp dir.
* `nointro.py`: `check_updates(names=None, opener=None, directory=None, timeout=10) -> list[dict]` = `{"name", "installed": version|None, "status": "up_to_date"|"update_available"|"missing"|"unknown"|"error", "error"?}` via HEAD ETag vs `manifest.json` (no stored ETag → `unknown`, treated as update); `download_dat` writes the manifest after the atomic `os.replace` (crash ⇒ a harmless re-download). Each No-Intro DAT is its own platform, so per-file atomicity is the unit.
* `platforms.py`: `Platform` adds `latest_dats: tuple[str,...] = ()`, `best_variant_dats: tuple[str,...] = ()` (Amiga: `("Commodore Amiga - Games - [ADF]",)` for both; `_nointro()`: `latest_dats=(dat,)`); exclusions apply to all `dats`, completeness to `m3u_dats`; public `source_of(platform) -> str`.
* `paths.py`: `updates_path() -> Path` (`data_dir()/updates.json`), `offline_forced() -> bool` (`ROMORG_OFFLINE=1`). Tests: `test_tosec/test_nointro/test_platforms/test_autoupdate` with fake openers only (no network).

## autoupdate.py (E, new)
```python
class UpdateError(Exception): code: str   # "offline" | "failed" | "cancelled"
class UpdateManager:
    def __init__(self, tosec=tosec, nointro=nointro, enabled: bool = True, clock=time.time, state_path=None)
    def start_background(self) -> None     # server startup: one daemon thread = check + download what is newer; idempotent; no-op when disabled
    def check(self, force: bool = False) -> bool       # "Check for updates" button; False if one is already running
    def cancel(self) -> bool
    def status(self) -> dict               # JSON below, thread-safe snapshot
    def ensure(self, platform, progress=None, cancel=None) -> None   # DATs of platform's source present? return. Else wait for the running update (poll, forwards progress) or run one here (single-flight); raises UpdateError
    dat_lock: threading.Lock               # held only while DATs are swapped in or parsed (milliseconds / a few seconds)
```
Flow: (1) *check* (timeout 10 s): `tosec.check_latest` + `nointro.check_updates`; network error → `offline=True`, keep cached DATs, `notice="Offline - using the installed DATs (checked <date>)"`; (2) *download* what differs: No-Intro files with status `update_available|unknown|missing`, then TOSEC when `latest != installed_release()` (a 100 MB pack only when the release date differs); (3) *commit* is atomic per source (TOSEC dir swap incl. release.json; No-Intro file replace) under `dat_lock`. State persisted to `updates.json` (`checked_at`, per-source `latest`).
**Concurrency rule**: the updater runs on its own thread (not a `JobManager` job) and never waits for or interrupts jobs; downloads/extraction touch only `cache/` and temp dirs; only the commit and the DAT *loading* of a scan (`App._platform_dats`) take `dat_lock`. A scan works on the DATs it parsed at its start; if a commit happens afterwards `status()["scan_stale"]` is true and the UI says "DATs updated - rescan". A scan/plan for a platform with no installed DATs calls `ensure()` first (progress forwarded to the job, job cancel cancels the update). Offline with nothing cached → `UpdateError("offline")` → job error code `offline_no_dats` (UI: clear message + Retry). Tests/smoke runs set `ROMORG_OFFLINE=1`/`App(auto_update=False)`.
`status()` JSON: `{"enabled", "state": "idle|checking|downloading|installing|error", "running", "offline", "notice", "last_checked", "scan_stale",
 "progress": {"done","total","message","source"}, "error": null|{"code","message"},
 "tosec": {"installed": "2025-03-13"|null, "latest": str|null, "status": "up_to_date|update_available|updating|absent|unknown|error", "checked_at"},
 "nointro": {"installed": "2026.08.01"|null, "latest": null, "status": ..., "dats": [{"name","version","status"}], "checked_at"}}`.

## server.py (D)
* `App(token=None, auto_update=True)`: `self.updates = autoupdate.UpdateManager(enabled=auto_update and not ROMORG_OFFLINE)`; `make_server`/`main` call `start_background()` after binding; `--no-update`. `_platform_dats` takes `updates.dat_lock`; `_run_scan` first calls `self.updates.ensure(platform, progress=job.report, cancel=job.cancel)` (UpdateError offline → `ApiError(503, ...)` with job `error_code: "offline_no_dats"`); `Job.to_dict()` adds `error_code`. No more "download the DATs first" 409. `ScanState` adds `library_plans`, `dats_changed`.
* New endpoints (POST need the token):
  * `GET /api/updates` → `status()` above. `POST /api/updates/check {force?: bool}` → `{"started": bool, "updates": status}`. `POST /api/updates/cancel` → `{"cancelled": bool}`. `POST /api/dats/update {force?, source?}` = alias of check (returns `{"started","updates"}`; the `{"job"}` form is gone). `GET /api/status` keeps `release`, `dats_count`, `nointro` and adds `updates`.
  * `GET /api/library/profile?platform=` and `POST /api/library/profile {platform, exclude?: [rule], latest_only?, best_variant?, complete_only?, reset?: bool}` → `{"platform", "profile": {exclude, latest_only, best_variant, complete_only}, "defaults": {..}, "rules": [{"key","label","on": bool}], "available": {"latest_only","best_variant","complete_only"}, "scopes": {"latest_dats","best_variant_dats","complete_dats","exclude_dats"}}`; persisted via `library.store_profile` into `config["library"]`; unknown rule → 400; clears `state.library_plans`. `GET /api/platforms` rows add `"library": <profile>`; `POST /api/platforms/options {latest_only}` writes `profile.latest_only`.
  * `POST /api/library/plan {move_unmatched?, savedisk?, labels?, status?, reason?, dest?, offset?, limit?, q?}` → paged `items` + `{"total","offset","limit","root","layout","profile","counts": {status: n}, "reasons": reason_counts, "playlists": {"write","ok","remove","conflict"}, "incomplete_sets": [{"name","dat","total","missing":[2,3],"present":[1]}] (max 200), "warnings", "actionable", "empty": bool, "missing_dats"}`.
    File rows = `_rename_item` + `"code"`, `"keeper"`, `"missing"`, `"flags_text"`, `"item": "file"`; playlist rows `{"item":"playlist","name","path","dir","lines","disks","status": write|ok|conflict|delete,"reason"}`. `reason` filter = `kept|excluded|superseded|incomplete|duplicate|unmatched|playlist`.
  * `POST /api/library/apply {move_unmatched?, savedisk?, labels?}` → job `library` (non-cancellable): `apply_renames(plan.ops, root, playlists=plan.playlists)`, result `{moved, deleted, playlists_written, failed, undo_log, action: "library", summary, rescan_error?}`; re-scan after. `POST /api/library/undo {log}` and `/api/organise/undo` share one implementation; `GET /api/organise/undo-logs` unchanged.
  * Existing: `/api/organise/plan|apply` = plain tidy (duplicates handled, no profile; `latest_only` legacy → profile alias; rows gain `code`…); `/api/m3u/plan|apply` = playlists only (slot-wise, `exclude_rules=profile.exclude`); `/api/convert/*` default `latest_only=profile.latest_only`; `_plan_warnings` counts rows with a reason `code` as matched, not as "unmatched".
* Tag filters: `TAG_FILTERS += ("rule",)`; `_tag_values(tags, "rule")` = `[e["rule"] for e in tags["excluded_by"]]`; `facets` adds `"rules": {key: n}`; the flag facet keeps key `bad` (UI label "Bad dump").
Tests (D): fake `autoupdate`/`library`/`m3u`/`organiser` modules in `sys.modules` as today; endpoint shapes above, 409/400 paths, job codes, no network (`auto_update=False`).

## UI (D) — `static/index.html`, `app.js`, `style.css`
* **Remove** the "Download DAT files" block (`#release-info`, `#nointro-info`, both download buttons, `#force-download`), the "press <button> below to download" notices and the `download` job handler. **Add** an updates bar at the top of step 1: one status line (`TOSEC 2025-03-13 · No-Intro 2026.08.01 · checked 14:02`), a small **Check for updates** button, a progress bar while `running`, a quiet "Offline - using installed DATs" note, "DATs updated - rescan" when `scan_stale`, and (offline, nothing cached) an error box with **Retry**. Poll `GET /api/updates` every 1.5 s while running, otherwise on load / focus. Scanning with missing DATs just starts the scan (its progress line shows the update).
* Steps become `1 Systems, 2 Scan, 3 Build library, 4 Convert, 5 Kickstarts`. Step 3: **Library rules** panel (checkbox per applicable rule from `rules`, plus Latest versions only / One best variant per game / Complete multi-disk sets only, each shown only when `available`; "Reset to defaults"; every change → `POST /api/library/profile`), buttons **Preview library / Build library / Undo last**, cards by reason (kept, renamed/moved, excluded, superseded, incomplete, duplicates, playlists to write/remove, conflicts), reason filter chips, table with reason text and exact flags, an "Incomplete sets" list showing the missing disk numbers, the standard confirm dialog. The old Organise and M3U steps move into `<details>` "Advanced: tidy only / playlists only" (same endpoints).
* **Bad dump**: `tagChips` renders `Bad dump [b corrupt file]` (class `tag-bad`, title = all `bad_flags`) from `tags.bad_flags`; other excluded rules render `label [exact flag]` (class `tag-excl`: "Modified [m baddump]", "Pre-release (pre-release)"); the tag bar gets a "Library rules" row from `facets.rules` and the `bad` facet is labelled "Bad dump".

## File ownership (disjoint) and build order
| Builder | Files |
|---|---|
| A | `romorg/tags.py`, `romorg/library.py` (new), `tests/test_tags.py`, `tests/test_library.py` (new) |
| B | `romorg/m3u.py`, `tests/test_m3u.py` |
| C | `romorg/organiser.py`, `romorg/scanner.py`, `tests/test_organiser.py`, `tests/test_scanner.py`, `tests/test_integration.py` |
| D | `romorg/server.py`, `romorg/static/index.html`, `romorg/static/app.js`, `romorg/static/style.css`, `README.md`, `docs/PACKAGING.md`, `packaging/*` (add `romorg.library`, `romorg.autoupdate` to the import self-check / smoke test), `tests/test_server.py`, `tests/test_packaging.py` |
| E | `romorg/tosec.py`, `romorg/nointro.py`, `romorg/autoupdate.py` (new), `romorg/platforms.py`, `romorg/paths.py`, `tests/test_tosec.py`, `tests/test_nointro.py`, `tests/test_autoupdate.py` (new), `tests/test_platforms.py` |
Not touched: `datfile.py`, `convert.py` (keeps `tags.superseded`), `kickstart.py`, `__main__.py`, `docs/ARCHITECTURE.md` (designer only).

Build order (land the stubs first, then fill in; everyone codes against the signatures above, using `getattr` fallbacks for another builder's not-yet-landed names):
1. **A**: `Tags.publisher`, `RULES`, `classify_token`, `exclusion_info`, `bad_flags`, `is_cracked`, `identity_key`, `group_version_keys`, `date_key`, `to_json` additions; `library.py` dataclasses + `LibraryProfile` (+ config helpers) + a `select()` stub that keeps everything. **E**: `Platform.latest_dats/best_variant_dats`, `source_of`, `UpdateManager` skeleton (`status/start_background/check/cancel/ensure/dat_lock` as no-ops), `paths.updates_path/offline_forced`.
2. **B**: `DiskCand/SlotSet/resolve_slots` (+ `PlaylistSpec`, `plan_playlists`, `plan_stale`, `write_playlist` stubs). **C**: `scanner.duplicate_groups`, reason constants, `RenameOp` fields, `reason_destination`, `LibraryPlan` shell.
3. **A** `select` (needs `m3u.resolve_slots`), **C** `plan_renames(profile=)` / `plan_library` / `apply_renames(playlists=)`, **E** tosec/nointro check + atomic release.json + the real update flow, **D** server endpoints against the stubs (fakes in tests).
4. **D** UI last (needs the JSON shapes only); everyone runs `python3 -m unittest discover -s tests`; then an end-to-end smoke with `ROMORG_DATA_DIR` temp + `ROMORG_OFFLINE=1` and a synthetic ABC library (build, re-plan = empty, undo).

### AMENDMENT 6 — note on TOSEC flag descriptors (user decision, 2026-10-02)
Per https://www.tosecdev.org/tosec-naming-convention a dump-info flag is `[x]` or `[x <free-text description>]` and each flag type may appear only once (except compilations). `[m baddump]` is therefore ONE modified flag with the description "baddump", NOT a modified + bad-dump pair. Classification uses the documented flag letter only; descriptors are free text and must never be keyword-matched (no "baddump"/"corrupt"/"virus" sniffing). The UI shows the exact flag text (e.g. "Modified [m baddump]"). Only `[b ...]` means bad dump.

### AMENDMENT 6 - implementation notes / deviations (integrator, 2026-10-02)
* Real-DAT numbers (all ROMs present, all rules on, `organiser.plan_library`): Games [ADF] kept 8601 / excluded 7645 / superseded 17917 / incomplete 247 files (76 games) / 1772 playlists; the whole Amiga platform kept 8840 (adds Workbench 85, Kickstart-Disks 38, Firmware 116), excluded 7828, incomplete 259, 1802 playlists. No-Intro kept/excluded/superseded: GBA 3205/339/148, N64 988/157/112, NES 5076/1579/415, SNES 3248/712/308. These replace the earlier 8550/17874/341 estimates (`select()` is deterministic and permutation-stable; the older figures came from a prototype). Full table: `library-sim.md` in the session scratchpad.
* Failure of one disk's move skips that playlist (reported in `failed`); `m3u.write_playlist` and `kickstart._write_no_overwrite` fall back to check-then-rename where hard links are unavailable (FAT/exFAT SD cards).
* `summary()["duplicates"]` == number of `duplicate` moves in the plan; files under any `_unmatched/<reason>/` count as correctly named. No-Intro alternate forms of one game (`.nes`/`.unh`) are not duplicates; two byte-order forms of one N64 game (z64 + v64) are (one is kept, the other goes to `_duplicates`).
* `ROMORG_OFFLINE=1` / `App(auto_update=False)` / `--no-update` disable the startup updater (tests and `smoke_test.sh`). Job error codes: `offline_no_dats`, `update_failed`, `no_dats`, `update_cancelled`.
* Workbench default rules exclude 56% of its disks (mostly `[m]` modified, per the TOSEC flag letter); users can switch the `modified` rule off per platform.
* Verified live: with release.json at the latest release nothing is downloaded (check only); with an old release and a blocked download the updater ends in state `error` (code `failed`), cached DATs and release.json stay untouched, the server stays responsive.

### AMENDMENT 7 - audit fixes (supersede Amendments 5/6 where they differ)
* **Keep-every-version DATs** (a TOSEC DAT in `m3u_dats` but not in `latest_dats`/`best_variant_dats`: Kickstart-Disks, Workbench, Firmware): `library.RESCUE_RULES = {modified, bad_size}`. An item excluded ONLY by `[m]`/`[o]`/`[u]` rules stays (decision `keep`, reason "kept although ...: the only dump of this version") when no non-excluded dump with the same release key exists (`library._release_key`: style, title, version, regions, languages, all `(..)` flags, i.e. the name minus its `[..]` flags). Bad dumps, viruses, pre-release, proto, demo, faked and unreleased are never rescued (alpha/beta Workbench and bad-dump-only Kickstarts stay out - the user's exclusions win). A rescued item that would be `incomplete` becomes a playlist-less `keep` (`Decision.missing=()`). On these DATs a disk next to a complete set of its group is always `keep`. `Decision.action == incomplete` never has an empty `missing` any more: `_missing_for` falls back to disk numbers with no dump-compatible disk (`m3u._compat`); a disk of a group with a complete set on a toggled-off Games DAT is `keep`.
* **Version tokens**: `tags._TOSEC_VERSION_RE` also takes a bare ` r219`, ` r01.1000`, ` rev1`, ` rev 2` title suffix as the version (same ordering as `Rev N`, `tags._version_key`). `Decision.elsewhere` + the reason text name missing disks that exist locally under a different country/language/edition (they are NOT mixed in - user decision; 161 of 259 real incomplete files were of that kind).
* **Flags**: `[a baddump]` classifies as `bad_dump`, `[inc. Virus]` as `virus` (free text is still not keyword-matched elsewhere; `[a virus removed]` is kept). `m3u._BUILD_NOTE` adds `[compilation ...]` (a different build, no slot borrowing across it); `_pick_slot` prefers a `[!]` disk on ties.
* **Reason-folder targets**: a move into `_unmatched/_<reason>/` whose target is taken (existing file, another op, a case-fold clash) gets `name (2).ext`, `(3)` ... (`organiser._free_reason_targets`, run first in `_resolve_conflicts`; reason text "renamed to ..."). So the spare always moves, the summary `duplicates` equals `plan_counts()["to_duplicates"]` and the plan settles in one round.
* **Symlinks** take part in `library.select` (`Item.link`) so they fill slots / rank, but are never moved (`skip`); a link whose rom is also present as a real file is dropped from the selection.
* `convert.plan_conversions` (and `summary()["convertible"]`) skip files under `_unmatched/_excluded|_superseded|_incomplete|_duplicates` (`scanner.is_in_reason_dir`).
* `plan_library`: a playlist removed because its disks are set aside says "set incomplete (missing disk N) - playlist removed; undo restores it" (or lists the reasons).
* **Updater**: `autoupdate._is_network_error` is true only for URLError / timeouts / ConnectionError / gaierror / SSLError / `http.client.HTTPException` / an OSError with a network errno; ENOSPC, EACCES, EROFS, ENOENT are a red `failed` error (no "Offline" notice). `nointro.update_dats` wraps unexpected per-DAT exceptions (and `HTTPException`, removing the `.part`) and re-raises the first cause when all fail. `tosec.extract_dats` refuses a pack with zero DATs before the swap (installed DATs untouched); `download_pack` rejects a body without the `PK\x03\x04` magic (deleted) and does not reuse a cached non-zip; `update_dats` deletes a corrupt zip and raises a friendly error. `tosec.update_lock()` (flock on `<data dir>/update.lock`, re-entrant per thread, `tosec.Busy` when another process holds it) wraps `UpdateManager._run`, `tosec.update_dats` and `extract_dats`; `recover_dats_dir` skips cleanup while another process holds the lock (Busy -> quiet notice). The server no longer ORs the manager-level `scan_stale` flag into `/api/updates` (it was never cleared): `scan_stale` is the precise per-scan DAT-signature check, and `_run_scan` calls `updates.clear_stale()`.
* UI: console Build library intro/layout hide the `_incomplete` / playlist wording for platforms without playlists; the "Bad dump" rule chip is hidden when it filters the same rows as the "Bad dump" tag chip; the rules summary has a space. README: cancel discards the partial pack (network errors / closing the app resume it).
* Real-DAT numbers (Amiga platform, all ROMs present, `library.select`): Games [ADF] kept 8590 / excluded 7650 / superseded 17923 / incomplete 247 (the 341 of the first estimate came from a prototype; `select()` is the reference); Workbench 115 kept (was 85+) / 103 excluded / 4 incomplete (partial Cloanto/DE disk 1 only, A3000 disk 4 only); Kickstart-Disks 37/6; Firmware 121/48; platform kept 8863, excluded 7807, incomplete 251, 144 incomplete sets; re-selecting the kept output changes nothing; No-Intro numbers unchanged.

---

# AMENDMENT 8 - platform versions, languages, keep-flags, region priority (core, no UI)

**Binding for the UI/server phase; supersedes Amendments 6/7 where it differs.** Code: `tags.py`, `library.py`,
`m3u.py` (slot partitions), `organiser.py` (reason counts), `platforms.py`. `server.py` / `static/*` are NOT changed
by this amendment (the UI agent wires the shapes below).

## Profile (`library.LibraryProfile`, persisted per platform in `config["library"][platform]`)
```jsonc
{ "exclude": ["bad_dump", "virus", ...],        // enabled exclusion rules (tags.RULES), unchanged
  "latest_only": true, "best_variant": true, "complete_only": true,   // unchanged
  "languages": ["En"],                          // ORDERED, most preferred first; [] = no language filter
  "keep_flags": ["a","cr","f","h","t","tr"],    // flag types that may stay; a missing type EXCLUDES its variants (Amiga Games only)
  "rescue_only_dump": false,                    // keep-every-version DATs: keep the sole [m]/[o]/[u] dump (DEFAULT OFF)
  "region_priority": ["Europe","USA","World","Japan"],   // No-Intro: best first; every other region follows A-Z
  "one_per_game": true }                        // No-Intro: exactly ONE version per game
```
* Python types: `languages: tuple[str,...]` (ordered - **deviation**: the brief said `frozenset`, but the profile list order
  is a ranking key; a set/frozenset is accepted and normalised English-first then A-Z), `keep_flags: frozenset[str]`,
  `region_priority: tuple[str,...]`. Constructor/`from_dict` normalise (unknown codes dropped, de-duplicated). Missing keys
  take the platform defaults, so profiles saved before this phase load with the new defaults; unknown keys are ignored.
* `default_profile(platform)`: Amiga = `languages ["En"]`, `one_per_game false`; each No-Intro console = `languages ["En"]`,
  `one_per_game true`, `region_priority [Europe, USA, World, Japan]`; `keep_flags` all six, `rescue_only_dump false` everywhere.
  `latest_only_profile()` (legacy flag) = no exclusions, `languages []`, `one_per_game false`.
* Scopes (new `Platform` fields): `language_dats` (Amiga: Games [ADF]; consoles: their DAT), `region_dats` (consoles). Keep-flags
  apply to `best_variant_dats` (Amiga Games). Workbench / Kickstart-Disks / Firmware are never language- or flag-filtered.
* `library.profile_info(platform, profile=None)` -> JSON for the panel:
  `{"platform","style":"tosec|nointro","profile":{...},"defaults":{...},"catalog":[rule_catalog],
   "available":{latest_only,best_variant,complete_only,one_per_game,region_priority,languages,keep_flags,rescue: bool},
   "scopes":{latest_dats,best_variant_dats,complete_dats,language_dats,region_dats,exclude_dats},
   "regions":[every region, priority first], "language_names":{"En":"English",...}}`.

## Rule catalog (`library.rule_catalog(platform_style)`, style = `"tosec"` | `"nointro"`)
Ordered list; entry = `{id, field, label, kind, default, tokens, description, applies_to}` (+ `default_value` for
`languages` / `region_priority`). Only entries applying to the style are returned (`applies_to` lists styles).
* `kind "exclude"` (9 entries, `tags.RULES` order): `id` = rule key, `field` `"exclude"`, `default` true (on = excluded), `tokens` =
  exact tokens as they appear in names, e.g. `["(pre-release)","(beta)",...,"[beta]"]`, `["[b]","[b ...]","[a baddump]"]`
  (`" ..."` = free text). No-Intro style lists the capitalised words No-Intro uses (`"(Beta)","(Proto)","(Demo)"`...) and only
  rules that have tokens there. Generated from the tables `tags.classify_token` uses (`tags._PAREN_WORDS`, `_BRACKET_RULES`;
  `tags.rule_tokens(style)`); `tags.token_rule(token)` classifies a catalog token. Tests enforce both directions.
* `kind "keep_flag"` (`cr h t a f tr`, tosec only): `field "keep_flags"`, `default` true (on = kept), tokens `["[cr]","[cr ...]"]`.
  Description of `cr` warns that unticking it drops ~a third of the games (they exist only as cracks).
* `kind "option"`: `latest_only` (both), `best_variant` (tosec), `complete_only` (tosec), `rescue` (field `rescue_only_dump`, default
  false, tosec), `one_per_game` (nointro), `languages` (both; `default_value ["En"]`), `region_priority` (nointro;
  `default_value` = the default order). `tokens` is `[]` for options.

## Available languages
`library.available_languages(source, platform=None)`, `source` = a `DatFile`, an iterable of DatFiles / `Item`s / `Rom`s.
Returns `[{"code":"En","name":"English","count":29269,"games":3808}, ...]`: English first, then `count` descending, then code.
`count` = releases (TOSEC rom names / No-Intro set names, nothing filtered out), `games` = distinct titles. Measured Games [ADF]:
En 29269, De 3135, Fr 1095, Pl 539, It 532, Es 263, Da 82, Cs 62, Nl 52, Sv 25, Tr 21.

## Language rules (`tags.variant_languages(t)`)
Languages a release can be played in: explicit language tag = all listed (`(de-en)`, `(En,Fr,De)`); no tag -> the country/region's
official language (`REGION_LANGUAGE` + `tags.EXTRA_REGION_LANGUAGE`: CH->De, BE->Nl, CZ->Cs, HU->Hu, HR->Hr, TR->Tr, IN->En,
AE->Ar, Scandinavia->Sv); no language and no country (TOSEC convention), or only a language-neutral region (World/Asia/Unknown)
-> `En`; TOSEC `(M3)` multi-language adds `En` (**added rule**: M-tags list no languages and virtually always include English);
`[tr <code> ...]` adds that language (`[tr en-de x]` adds En and De). A variant is eligible if it has >=1 selected language
(`languages == []`: everything). Language is NOT part of the identity.

## Identity, platform ranking (Amiga Games, `best_variant`)
* `tags.identity_key` (TOSEC) = `(style, title, countries, publisher, edition flags, status tokens)`; **languages and bare chipset
  tags (`AGA`, `OCS`, `ECS`, `OCS-AGA`, `ECS-AGA`, `OCS-ECS-AGA`, `CD32`) are no longer in it** (`tags.is_chipset`). Labelled disks
  (`(AGA Data)`, `(Car Disk 1 AGA)`), `(M3)`, publisher, country, `World Cup Edition` titles still separate games.
* `m3u.resolve_slots` partitions its candidates by `tags.partition_key` = (explicit languages, chipset tags) so disks of different
  languages / chipsets never share a playlist (the stand-alone M3U step and the library behave the same). `library` sets carry
  `lang_rank`, `platform_rank`, `part`.
* Ranking of the eligible COMPLETE variants of one game, best first: (0) best selected language (profile order; irrelevant for one
  language), (a) cracked, (b) platform `tags.PLATFORM_ORDER = ("CD32","AGA","OCS")` (class of the chipset tags; untagged/OCS/ECS =
  `OCS`; add CD32 by putting a `CD32` chipset tag in names - no code change), (c) newest version, (d) fewest `[t][h][tr][f][a]`,
  (e) name. Latest-only without best variant keeps the newest set per (dump flags, language, chipset) signature.
* Measured (all roms present, defaults): 85 games have both AGA-class and OCS-class variants, the kept one is AGA in 73; 12 games switch
  from the old pick (cracked > newest) to AGA. Alien Breed II -> AGA `[cr FLT][h Triangle]` 2-disk set (the AGA 4-disk crack has a bad
  disk 2); Alfred Chicken, Air Bucks -> AGA sets (no crack exists); Anstoss World Cup Edition (English + German selected) -> AGA `[cr PDY][HD]`;
  Anstoss (1993) is German-only and vanishes under English-only.

## Keep-flags
`tags.flag_types(t)` finds `cr h t a f tr` (`[tr de]` is a `tr`, never a `t`). A flag type missing from `profile.keep_flags` excludes the
variant (code `flag_<x>`, detail = exact text `[cr FLT]`). Excluded flags never force keeping more than one variant per game. Ranking
(1d) still puts hacks/trainers below clean variants. **Warning shown to the user:** unticking `cr` excludes 20434 files and makes 1994 titles
vanish (kept games 3578 -> 1625); the preview's vanish report lists them with reason `flag_cr`.

## Consoles (No-Intro): one version per game
`tags.game_key(t)` = (title, status, licence/publisher/hardware/event/disk tags, dump flags): regions, languages, versions, dates, `Alt`,
`PAL`/`NTSC` and re-release tags (`Virtual Console`, `Collection`, `Switch Online`...) are NOT in it; `(Unl)`, `(Aftermarket)`, `(Pirate)`,
`(Tengen)`, `(Beta)`/`(Beta 2)` stay separate products. With `one_per_game` ONE variant per (game, header form) is kept: (0) best selected
language, region rank (`tags.region_order(region_priority)`: priority list, then every other region A-Z; a multi-region release ranks by its best
region), newest version of that region, fewest extra tags (`tags.extra_tag_count`), name. The others are `superseded` (`superseded_by` = winner).
`one_per_game` false = the old latest-per-region. Measured defaults (En): kept GBA 1438, N64 402, NES 2650, SNES 1110 (previous 3205 / 988 / 5076 / 3248,
reproduced exactly with `languages []`, `one_per_game false`); titles vanishing for language 840 / 183 / 1539 / 1245.

## Reason codes, counts, vanish report
* `Decision.action == "excluded"`, `Decision.codes` = every code that applies in order: rules (`bad_dump virus bad_size pre_release prototype
  demo faked unreleased modified`), `flag_cr flag_h flag_t flag_a flag_f flag_tr`, `language`. `Decision.detail` = exact texts
  (`"[cr FLT]"`, language: `"(De)"` = the languages the file has). `library.ALL_CODES`, `library.eligibility(name, style, profile, languages, flags)`.
  Files go to `_unmatched/_excluded/` as before (no new folder).
* `organiser.RenameOp.reasons: tuple[str,...]` (the codes; first = primary). `organiser.reason_counts(plan)` adds always-present int keys
  `excluded_<code>` for every code (and `excluded_other`), split by primary reason, summing to `excluded` (moves only, like the other keys).
  `LibraryPlan.exclusion_counts()` -> `{"exclusive": {code: files}, "any": {code: files}}` over all excluded decisions (also files already in `_excluded`).
* Vanish report: `Selection.vanished: list[Vanished(dat,title,name,reason,codes,languages,variants)]` (a title = `tags.title_key`: TOSEC title + publisher,
  any country/language; No-Intro `game_key` w/o status) with NO kept variant; `reason` = primary (language first, then `flag_*`, rules, `incomplete`),
  `codes` = codes that block a variant on their own. `LibraryPlan.vanished`, `.vanish_summary()` -> `{"titles","by_reason","by_code"}`;
  `library.vanish_report(selection, limit=200, reason="")` -> `{"titles","by_reason","by_code","items":[{"dat","title","name","reason","codes",
  "languages","variants","text"}]}`, `text` e.g. `"no version in selected languages (has De)"`. Measured Games [ADF] defaults: 1154 titles vanish
  (934 language, 118 demo, 22 bad_dump, 29 unreleased, 22 pre_release, 15 incomplete ...).

## Rescue (`rescue_only_dump`)
The Amendment 7 rescue is now OFF by default: Workbench defaults = 90 kept / 125 excluded / 7 incomplete (rescue on: 115 / 103 / 4). Kickstart-Disks
37 kept / 6 excluded and Firmware 116 / 53 are unchanged. Only exclusions by `[m]`/`[o]`/`[u]` alone are ever rescued.

## Numbers (all roms present, Amiga Games [ADF], `select`)
Defaults (En, all flags): kept 6132 files = 3578 games, 1244 playlists; excluded 11772 (language 4122, bad_dump 2815, modified 2774, virus 686, demo 661,
pre_release 426, bad_size 181, unreleased 78, faked 20, prototype 9); superseded 16386; incomplete 120. En+De: kept 7158 / 3994 games. No language filter: kept 8191 / 4459
games. Re-selecting the kept output changes nothing (also property-tested with random profiles).


# AMENDMENT 9 - reason folders at the root level

**User decision (2026-10-02); supersedes every statement in Amendments 3-8 that puts a reason folder
*inside* `_unmatched/`** (`_unmatched/_excluded/`, `_unmatched/_superseded/`, `_unmatched/_incomplete/`,
`_unmatched/_duplicates/`, `_unmatched/_converted_originals/`, and `SUPERSEDED_DIR` /
`CONVERTED_DIR` strings of the form `_unmatched/_...`). Everything else in Amendments 6-8 (rules,
ranking, counts, preview categories) is unchanged.

## Layout

All six reserved folders are direct children of the platform root:

```
<root>/
  _unmatched/            only files that matched nothing (relative sub-paths kept, as before)
  _excluded/             bad dumps, betas, demos, language / flag exclusions (library rules)
  _superseded/           older versions / worse variants
  _incomplete/           multi-disk games with missing disks
  _duplicates/           extra copies of the same ROM
  _converted_originals/  originals kept by Convert
```

Each folder keeps the file's path relative to the root inside it (`in/x.adf` -> `_excluded/in/x.adf`).
A file that moves between reason folders keeps its core path (the leading reserved folder is dropped).
Collision handling (`name (2).ext`), keeper choice, idempotence and "moves back when it no longer
qualifies" are unchanged.

## The reserved-name constant

`romorg/folders.py` is the only place that lists the names: `RESERVED_DIRS` (the six above),
`REASON_DIRS` (`_excluded`, `_superseded`, `_incomplete`, `_duplicates`), `LEGACY_SUBDIRS`, `CODE_DIRS`
(op code -> folder) and the helpers `classify(parts)`, `reserved_of`, `top_folder`, `is_reason`,
`is_converted`, `core_parts`. It imports nothing from the package. `organiser`, `scanner`, `convert`
and `server` re-export / import those names (no second list anywhere; the UI gets
`reserved_dirs` in the plan responses and keeps display strings only). Names compare
case-insensitively (exFAT): `_Excluded/` is `_excluded/`. Only the **top level** is reserved:
`games/_excluded/x.adf` is an ordinary path.

## Semantics

* `scanner.is_converted_original / is_in_reason_dir / is_set_aside / is_set_aside_duplicate` test the
  top-level folder (and the legacy place, below). The duplicate keeper ranking treats every reserved
  folder like `_unmatched/` ("under a reserved folder" ranks after a normal place).
* `organiser.reason_destination(src, root, dir)` = `root/<dir>/<core path>`.
* `RenameOp.folder` (new) = the reserved top-level folder of the target ("" = a normal place).
  `unmatched` stays "target is a reserved app folder". `plan_counts()["to_unmatched"]` and
  `reason_counts()["unmatched"]` now count only moves into `_unmatched/`; reason moves are counted in
  `to_excluded / to_superseded / to_incomplete / to_duplicates` (previously `to_unmatched` included
  them). Preview `dest` values are the top-level folder of the target, so the destination chips show
  `_excluded/`, `_superseded/`, `_incomplete/`, `_duplicates/` and `_unmatched/` separately.
* Empty-dir cleanup protects root, DAT folders and `_unmatched/` only; an emptied reason folder is
  removed (as before) and undo recreates it.
* The "too broad folder" warnings count only `dest == _unmatched` as unmatched; files going to / sitting
  in the other reserved folders are neither "matched" nor "unmatched".
* Name collisions with user folders: a normal folder the user already named like a reserved one is
  *by design* that reserved folder. ROMs inside it are matched and re-evaluated by the canonical
  rule: if nothing excludes them they move back to their canonical place; if a rule excludes them
  they stay (or only the spelling of the folder is normalised on a case-sensitive disk). Unmatched
  files in it stay. Documented in the README ("Reserved folders").

## Legacy migration

Libraries built with Amendments 3-8 have files under `_unmatched/<reserved reason folder>/...`.
`folders.classify` reports such paths as `(<folder>, legacy=True)`, so the scanner sees them as being in
the reason folder and the planner re-evaluates them like any file in one:

* matched, still excluded / superseded / incomplete / duplicate -> the new top-level folder;
* matched and now qualifying -> the canonical place;
* unmatched file in a legacy reason folder -> the same-named top-level folder (it keeps its reason
  code, so previews count it there; done even with "move unmatched" off, because these folders are
  the app's own);
* anything in `_unmatched/_converted_originals/` -> `_converted_originals/` (status `move`; a
  non-legacy original stays `ok`).

Each of those is an ordinary previewed, undoable move with the reason text
`moved out of _unmatched/_excluded/ (new layout)` appended. The existing empty-dir cleanup then removes
legacy dirs that our moves emptied (not `_unmatched/` itself, not dirs that were already empty).
A migrated library plans empty on re-run. Old undo logs (v3) store relative paths and undo unchanged
(covered by a test that hand-writes a log of the old layout). Convert recognises legacy originals as
converted originals (never converts them again) but does not move them; the next Build library does.
New converts write their original to `_converted_originals/<rel>` (a leading reserved folder is dropped).

## Tests

`tests/test_layout.py` (new layout per reason, legacy migration + idempotence + byte-exact undo, v3 log,
case-insensitive names, scan/summary ignoring reason folders, convert original location) plus the
layout paths updated throughout the existing suites.
