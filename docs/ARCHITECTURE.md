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


# AMENDMENT 10 - "Commodore Amiga - WHDLoad" as a separate system; folder persistence; config writes

**Binding; supersedes Amendments 1-9 where it differs.** USER RULE: the WHDLoad system shares NOTHING with
"Commodore Amiga" (TOSEC ADF): own folder, own DAT source/directory, own config keys, own library profile,
own Kickstart step, no shared identity or cross-DAT logic. (Only generic code is shared.)

## A. Folder persistence bug and config writes
* Cause: `static/app.js` only persisted a system's folder on the small **Save** button or on a successful scan;
  Browse / Folders / typing set a draft (`state.drafts`) that died with the page. Now the field saves on
  `change` (blur / Enter after an edit), after Browse / Folders (they dispatch `change`), before a scan
  (`startScan` awaits `commitFolder`; a refused path aborts the scan with the reason) and via **Clear**. Saves are
  serialised per system (`folderQueue`), the newest text wins, each row shows *Saved / Not saved / Saving /
  error (tooltip = reason)* (`data-folder-state`), a toast confirms. The Save button is gone.
* `paths.update_config(mutate)` is the only way to write `config.json`: one process-wide `RLock`, read the LATEST
  file inside the lock, apply, atomic replace (fsync). A damaged file is copied to `config.json.damaged` first.
  `App._config_update`, `_save_profile` (was a stale-snapshot read-modify-write), scan, folders, options and
  Kickstart saves all use it. `folders_save` / `kickstart/dest` run `strict` (a failed write is a 500, not an
  empty answer). `paths.save_config` stays for whole-file replacement only.
* Kickstart destination is per platform: `config["kickstart_dests"] = {platform: path}`; the legacy single
  `kickstart_dest` is migrated (on every config write and when read) to `kickstart_dests["Commodore Amiga"]` ONLY.
  `GET /api/status` keeps `kickstart_dest` (= the Amiga's) and adds `kickstart_dests`. New
  `POST /api/kickstart/dest {platform, dest}` saves the choice immediately.

## B. DAT source: `romorg/whdload.py`, `paths.whdload_dir()` (= `data_dir()/whdload`)
Mirror of `nointro.py` for ONE DAT: `DAT_NAME = "Commodore - Amiga - WHDLoad"` (header name = file stem),
`BASE_URL` raw.githubusercontent MrV2K/WHDLoad-Database/main, `header_version` (header `date`, else `version`),
`manifest.json` with the ETag, `list_dats / find_dat / download_dat / check_updates / update_dats` with the same
result shapes (`source: "whdload"`), `WhdloadError`. Download -> `.part` -> parse (>=1 rom, header name equal) ->
atomic replace -> manifest. Offline = cached DAT. No licence is stated for the repository: only the DAT (hashes) is
downloaded. `datfile.parse_clrmamepro` falls back to the header `date` as `version` and decodes a non-UTF-8 file
as Windows-1252 (the real DAT has `Alien\xb3`, `D\xe9but` in game names; rom names are ASCII).
`autoupdate.UpdateManager(..., whdload=whdload)`: a third source: check (HEAD ETag), download after No-Intro,
commit under `dat_lock`, scope `("whdload", names)` for `ensure(platform)`, `status()["whdload"] = {installed,
latest, status, dats, checked_at}`, persisted `checked_at`. `whdload=None` = not managed (tests with fakes).

## C. Platform
`Platform` gains `kickstart_folder: str = ""` and `protected_dirs: tuple = ()`; `DatSource.WHDLOAD`;
`platforms.has_kickstart(p)` (= `kickstart_dat or kickstart_folder`); `locate_dats(..., whdload_directory)`.
`"Commodore Amiga - WHDLoad"`: dats `("Commodore - Amiga - WHDLoad",)`, source `whdload`, layout `flat`, no M3U,
extensions `.lha .lzx`, `folder_hint "whdload"`, `latest_dats = best_variant_dats = language_dats = (dat,)`,
`region_dats = ()`, `kickstart_folder "Kickstarts"`, `protected_dirs ("Kickstarts",)`. DAT roms are loaded with
`set_names=True`: set = the rom stem, so the same `.lha` listed under several game names (9 such files in the real
DAT) is ONE unit (have / missing / duplicates count sets: 4112 sets for 4121 entries).
* Matching: the scanner hashes `.lha` / `.lzx` as plain files (sha1, crc + size). Only `.zip` (zipfile) and
  `.7z` / `.rar` (7z) are archives; a test feeds a `.lha` that is a valid zip and checks it is never opened.
* Organise = rename to `rom.name`, flat, unmatched -> `_unmatched/`, reason folders as in Amendment 9.
* `folders.py`: `KICKSTARTS_DIR`, `PROTECTED_DIRS`, `is_protected(parts, names)`. A PROTECTED name is not a
  reserved/reason folder: `scanner.scan(..., protected_dirs=platform.protected_dirs)` / `collect_files(protected=)`
  skip that top-level folder (case-insensitive), so scan / organise / Build library / Convert never see it; other
  platforms are unaffected (an Amiga TOSEC root's `Kickstarts/` is scanned as before).

## D. Tags (`tags.py`): style `whdload`
`STYLE_WHDLOAD`, `WHDLOAD_DAT_NAME`, `parse_whdload(stem, game="")` (lru-cached), `style_of_rom(rom)`,
`of_rom(rom)` (`Rom.tags` uses it; `organiser` Item style too), `Tags.build: int = 0`. Only the DAT name selects the
style. Measured on the real DAT (4121 entries; game-name parens / stem tokens, counts):
* **Game name** (identity, status, product tags): language words German 280, French 211, Italian 106, Spanish 61,
  Polish 38, Danish 9, Czech 5, Swedish 5, Greek 3, Finnish 3, Dutch 1, Croatian 1 -> `languages`; chipset AGA 269, CD32 133,
  OCS 2, ECS 2 (`PLATFORM_ORDER` CD32 > AGA > OCS; untagged = OCS); NTSC 252; memory 512KB 40 / 512k 10, 1MB 31,
  2MB 7, Fast Mem 34, Low Mem 16, Chip Mem 8, Slow Mem 3, 1.5MB, 8MB, 12MB, 1MB Chip; status Beta 138, Pre Release 4,
  Preview 2, Game Demo 73, Demo 1, Unreleased 2 (Playable / Rolling / Playable Demo N only next to Game Demo); product
  tags (all kept in the identity): Cover Disk 80, PD 76, Files 39, Image 39, CDTV 25, MT32 24, Two Disk 18, One Disk 17,
  Three/Four Disk, CD-ROM 18, Arcadia 17, Enhanced 14, Hack 5, No Intro 7, Hi/Low Res, ST Port, Alt 3, Censored, Crunched,
  publishers / magazines / editions (free text, ~150 distinct).
* **Archive stem** `Title_v1.2[_Lang][_AGA][_1MB]..._0417`: version `vX.Y[a]` (4118 of 4121; `v2.1-B` -> `v2.1b`; 3 have
  none), 4-digit build (2206; `0107&0266` = max), language codes De 278, Fr 209, It 106, Es 61, Pl 38, Dk 9, Cz 5, Se 5,
  Gr 3, Fi 3, Nl 1, Hr 1 and multi-codes `EnFrDe` / `EnFrEs`, AGA 269, NTSC, CD32 131, CDTV 25, ECS/OCS, `512k` `512KB`
  `512Kb` `1MB` `1Mb` `2MB` `15MB` `1MbChip` `Fast` `Slow` `Chip` `LowMem`, `BETA` / `Beta3` / `PreRelease`, `Hack` /
  `fix` + `by_<author>`, `Image` `Files` `1Disk` `2Disk` `3Disk` `4Disk` `CD` `NoIntro` `LoRes` `HiRes` `AtariST` ... Free
  words (publishers `Ocean`, `Psygnosis`, authors) that the game name also carries are not parsed twice.
* Merge: game-name values + stem values (union; they agree on every language / chipset / NTSC / memory token of the DAT;
  `Beta` is mostly game-name only: 138 vs 10 `BETA` stems, which is why the game name must be consulted). Hack / fix
  with `by <author>` adds `Hack` and `by <author>` flags (different authors are different products).
* Rules: `_WHD_PAREN_WORDS` (own vocabulary, used only when `Tags.style == "whdload"`): pre_release = Beta, Pre Release,
  Preview; demo = Game Demo, Demo, Playable Demo; unreleased = Unreleased. No prototype / bad dump / modified / virus tokens
  exist, so those catalog entries do not apply (`rule_tokens("whdload")`, `classify_token(.., style)`, `token_rule(.., style)`).
  NOT excluded (kept, separate products): Cover Disk, PD, Hack, Alt, Two/One Disk, Image/Files, CDTV, CD-ROM, Enhanced.
* `identity_key` = `(style, title.casefold(), status.casefold(), sorted product flags)` (`whd_identity_key`); language, chipset,
  NTSC, memory, version, build are ranking attributes. `title_key` = (style, title). `flag_kind`: CD32 / CDTV / MT32 and
  memory tags count as `hardware` (facet chips).

## E. Library (`library.py`)
`_select_whdload` (items of style whdload; scope `latest_dats` / `best_variant_dats`):
* eligibility: exclusion rules + language filter (`variant_languages`; no tag = English). Keep-flags do not apply.
* `best_variant` (default): per game keep ONE: lowest of (language rank, platform rank CD32 > AGA > OCS, memory rank
  [none 0 < 1MB/2MB/Fast/Slow/Chip 1 < 512KB/512k/Low Mem 2], NTSC 1), then newest version (zero-padded fractions as in
  `tags.group_version_keys`), then highest build, then name. Others -> `superseded` with the first deciding criterion in
  the reason text. `latest_only` without it: newest (version, build) per variant signature (languages, chipset tags,
  memory tags, NTSC); both off: everything kept. Pure and location independent -> idempotent (tested on the real DAT).
* Catalog (`rule_catalog("whdload")`): pre_release, demo, unreleased (exact tokens), `latest_only`, `best_variant`,
  `languages` with WHDLoad descriptions; keep-flags, complete_only, rescue, one_per_game, region_priority do not apply
  (`applies_to`; `profile_info.available.keep_flags` is False for it). `available_languages` and the vanish report work
  through `tags.of_rom` / `_tags_of`.
* Real DAT, defaults (4112 sets, all present): kept 2763 / excluded 924 (language 711, pre_release 137, demo 74,
  unreleased 2) / superseded 425; 376 titles vanish (language 269, pre_release 82, demo 24, unreleased 1). En+De: kept 2852;
  no language filter 3044; latest-only (best off) 3188; everything off 4112. Re-selecting the kept output changes nothing.

## F. Kickstart per platform
`kickstart.plan_kickstarts_from_folder(folder, dest)` / `scan_kickstart_folder(folder)`: every file under the folder
(recursive, hidden skipped, <= 1.1 MB) is MD5-hashed and matched against `PUAE_BIOS` (no DAT, no TOSEC data); ops
`copy | ok | conflict | missing` as before plus `unmatched` (any other file: reported, never copied). The TOSEC planner
(`plan_kickstarts`, source = Firmware DAT) is unchanged (they share only `_plan_from_local`). Server: `kickstart/dirs`
(`?platform=`; `last` per platform, `kickstart_folder`), `kickstart/plan|apply {platform, dest, ...}`, `kickstart/dest`.
A DAT-based platform needs a scan OF THAT platform (409 otherwise); a folder-based one needs its platform folder (saved
or scanned) - missing `Kickstarts/` yields an all-`missing` plan with `source_exists: false`. Responses add `platform`,
`source` (`dat|folder`), `source_dir`. `GET /api/platforms` rows add `kickstart_folder`, `has_kickstart`, `kickstart_dest`,
`protected_dirs`. UI: the step shows per selected system (own destination field, detected dirs, intro text, source note,
`unmatched` chip) and is hidden when `has_kickstart` is false.

## G. Judgment calls
(1) CD32 > AGA > OCS also when the OCS entry is newer (Exile v1.4 OCS loses to Exile v1.1 CD32) - as specified.
(2) CDTV, CD-ROM, Image/Files, nDisk, Hack, Cover Disk, PD, Alt, publishers are product tags (identity), not variants.
(3) `(Beta)` (138 entries, many are hacks/ports) is excluded by default through the existing pre_release rule; Hack, Cover Disk
and PD are not excluded. (4) 1MB / 2MB / Fast / Slow / Chip memory builds rank equally (between standard and 512KB / Low Mem);
ties fall to PAL, version, build, name. (5) The stem `No` is not Norwegian ("No_jump"). (6) Same title spelled differently
(`Super Skid Marks` vs `Super SkidMarks`) stays two games. (7) Only loose files in `Kickstarts/` are matched (archives are not
opened there). (8) `.zip` / `.7z` files in the WHDLoad folder are still treated as archives by the generic scanner.


# AMENDMENT 11 - Sega Dreamcast (Redump, CHD)

**Binding; additive.** Amiga / WHDLoad / the consoles are unchanged; the new system shares no DAT, folder, config key
or matching with them. Stdlib only. New modules: `redump.py`, `chd.py` (+ `flacdec.py`, `cdecc.py`), `chdtool.py`,
`chdpool.py` / `chdworker.py`, `dreamcast.py`. Tests: `test_chd`, `test_redump`, `test_chdtool`, `test_chdpool`,
`test_dreamcast`, `test_dc_server` (+ `tests/chdtestlib.py`: a **test-only** CHD v5 writer, tiny FLAC encoder, synthetic
discs, Redump-style DAT writer and a fake `chdman` shell script).

## Source, platform, layout
* `DatSource.REDUMP = "redump"`, `paths.redump_dir()` (= `data_dir()/redump`, own folder). `redump.py` mirrors `whdload.py`:
  `DAT_NAME = "Sega - Dreamcast"`, `check_updates` (HEAD, falling back to a header-only GET; version = the date in the
  `Content-Disposition` file name `... (2026-06-14 18-25-41).zip`; status `update_available` only when the remote date is
  **newer** than the installed DAT `<version>`), `download_dat` (zip -> `.zip.part` -> single `.dat` member copied by bytes
  (no path from the zip) -> parsed, header name checked -> atomic replace -> `manifest.json`), `update_dats`, `list_dats`.
  **HTTP only** (`http://redump.org/datfile/dc/`; HTTPS is refused). `autoupdate.UpdateManager(..., redump=redump)` is a
  fourth source (check phase, download phase after WHDLoad, scope `("redump", names)` for `ensure`, `status()["redump"]`,
  persisted `checked_at`, `RedumpError` counts as a network error like the others); `redump=None` = not managed.
* `Platform "Sega Dreamcast"`: `source redump`, `layout "game_folder"` (`platforms.LAYOUT_GAME_FOLDER`), extensions
  `.chd .gdi .cue .bin .raw`, `convertible=True` (raw -> CHD), `m3u_dats=()` (playlists belong to Build library),
  `latest_dats = language_dats = region_dats = (DAT,)`, folder hint `dreamcast`. `platforms.load_platform_dats` parses it
  with `datfile.parse_redump` (every rom of a game gets `set_name = game`: one unit = one disc; `Rom.category` new last field
  = the `<category>`).
* Layout: `<root>/<Redump name>/<Redump name>.chd` + sidecars; the six reserved folders as before.

## chd.py: pure-Python CHD v5 reader
`Chd(path, load_map=True)` (header + metadata always; the map lazily when `load_map=False`): header (124 bytes, version 5
only, parent -> `ChdUnsupported`), compressed map (16-byte header, Huffman tree RLE-coded with 16 codes / 8 bits, types with
RLE_SMALL / RLE_LARGE, lengths / offsets / CRC16, SELF / SELF0 / SELF1; the map CRC16 over the rebuilt 12-byte entries is
verified) or uncompressed map; hunk codecs `cdlz` (raw LZMA1 lc3 lp0 pb2 + deflate subcode, ECC bitmap), `cdzl`, `cdfl`
(FLAC frames + deflate subcode), plus `zlib`, `lzma`, uncompressed (zstd / huff / parent -> `ChdUnsupported`). Sectors of
frames whose ECC bit is set get sync + P/Q parity rebuilt (`cdecc.generate`: ECMA-130, vectorised with big integers: P rows
and the Q diagonals via strided slices + SWAR GF(2^8) arithmetic, 8-30 us per sector; verified against the per-sector
reference `generate_reference` and the real files). `flacdec.decode_frames`: fixed / LPC / constant / verbatim subframes,
all stereo modes, Rice partitions, CRCs not checked. CD audio is big-endian in hunks; extraction swaps it back.
Metadata `CHT2 CHTR CHGD CHGT` -> `Track(number,type,subtype,frames,pad,pregap,pgtype,pgsub,postgap,start,gd)`; track `start`
= running sum of `frames` each rounded up to a multiple of 4 (chdman's track padding). **Extraction = chdman `extractcd`**:
`data_frames = frames - pad` (GD-ROM pad frames are at the END of the track's frames and dropped), 2352-byte sectors
(MODE1_RAW / MODE2_RAW / AUDIO; the cooked types give 2048 / 2336 / 2324 bytes at offsets 16 / 24), subcode dropped. A virtual
pregap (`PGTYPE` starting with `V`) contributes nothing; a stored pregap is part of `frames`. (Pregap / CD-with-pregap
semantics are derived from chdman's documented behaviour; the only real files here are GD-ROMs with pregap 0 - **not
validated on a real CD CHD**.) `iter_track`, `hash_track` (crc32 + md5 + sha1 in one pass), `verify_raw_sha1`. Hunks are
decoded in groups of 16 so the ECC work is batched; nothing is loaded as a whole.
Measured on the real files (Steam Deck, one core): `cdlz` data 95 MB/s decode (76 MB/s hashed end to end), `cdzl` ~41 MB/s,
`cdfl` (pure-Python FLAC) 1.2 MB/s, rawsha1 path (with subcode) ~37 MB/s.

## chdtool.py (chdman)
`detect(config)` (order: `$ROMORG_CHDMAN`, config `chdman_path`, `PATH`, Flatpak `org.mamedev.MAME` / `net.retrodeck.retrodeck`
(`flatpak run --command=chdman`), `~/.local/bin`, `~/Emulation/tools[/chdconv|/MAME]`, `~/retrodeck/tools`, `~/bin`, `/usr/bin`,
`/usr/local/bin`; each probed with `chdman help`), `info()` (UI JSON incl. install steps), `extract_cd` (GD-ROM -> `disc.gdi` + track
files parsed from the gdi; CD -> `disc.cue` + one bin sliced by the CHD's track sizes), `create_cd`, `run` (progress from the
`NN.N%` output, reader thread + prompt cancel: SIGTERM then SIGKILL of the process group), `hash_range`, `check_space`,
`make_workdir` / `remove_workdir` / `sweep_stale`. Temp files: hidden `<root>/.romorg-chd-<id>/` with a `pid` file **inside the
ROM folder** (same file system, never `/tmp`); `sweep_stale` (server start for the saved Dreamcast folder, every scan and convert) removes
folders of dead pids or older than 24 h. Flatpak access: `flatpak info --show-permissions` is checked and the error carries
`flatpak override --user --filesystem=<dir> <app>`. The real chdman could **not** be run here; tests use a fake shell script.

## dreamcast.py
* **Index**: `DcIndex(dat)`: games (`DcGame`: `(Track N).bin` roms in track order, `.cue` kept but ignored), `by_sizes` (track-size
  tuple -> games; 945 tuples for 1516 games, 105 shared), `disc_totals()`.
* **Units** (`discover_units`): a CHD alone in a folder that is not the root / a reserved folder = the whole folder (recursive,
  hidden files included, our own playlists / temp excluded, sub-folders holding their own CHD excluded); otherwise the CHD +
  the files of its directory that start with its stem (longest stem wins). Raw sets: a `.gdi` (else `.cue`) in a folder without
  a CHD (one file per track); sets inside `_converted_originals/` are `result.originals`, not matches.
* **Matching** (`match_chd`): candidates by exact track-size tuple, then every DATA track smallest first (early exit) must equal
  the Redump rom (sha1 + crc + md5), several candidates -> audio hashed to tell them apart; level `verified` only when all tracks
  hashed and equal, else `identified`. Hash source: chdman `extractcd` when found (`engine auto`; the extracted sizes must equal
  the metadata, no space / any chdman error -> pure Python), else the reader. `ChdCache` table `chd_hashes(path,size,mtime_ns,sha1,
  kind,tracks,level,via)` in `hashes.sqlite` (key = path, size, mtime_ns, CHD header sha1); a cached unchanged CHD is never decoded.
  A fully hashed CHD with one differing track is *unmatched* with the track numbers in the reason.
* **`scan(...) -> DcScanResult(ScanResult)`**: `matched` = `DcMatch(Match)` (`unit`, `level`, `kind chd|raw`, `item()` row), `unmatched` =
  `DcEntry` (`reason`, `kind`), `missing` = one stand-in `Rom` per missing game, `units`, `junk`, `originals`, `engine`; `summary()` keeps the
  generic keys and adds `chd_files identified verified raw engine chdman`. `workers > 1` (server: auto, up to 4; config `chd_workers`,
  `$ROMORG_CHD_WORKERS`): CHDs are identified concurrently and their tracks hashed by `chdpool.HashPool` worker processes
  (`python -m romorg.chdworker`, JSON lines, killed on cancel, any worker failure -> in-process fallback; POSIX only).
* **Plans**: game-level `DcOp(RenameOp)` (`moves` = file moves, `game`, `level`, `n_files`, `unit`); `plan_tidy`, `plan_library(result, profile,
  move_unmatched, savedisk, labels)` -> `organiser.LibraryPlan` (ops + playlists + `library.Selection`); `apply_plan` expands to file ops and calls
  `organiser.apply_renames` (one journal, one undo, never overwrites, empty folders removed). Canonical target = `<root>/<safe name>/` with the
  folder, the CHD and every file starting with the old stem renamed; unrelated files keep their names and move with the folder; reason folders
  keep the current names (`_excluded/<folder>`, ` (2)` suffix on collisions); duplicates: canonical > outside reserved folders > verified > the copy
  with the most files (the saves) > shortest path; conflicts (target taken) skip the whole game.
* **Library rules**: `tags.STYLE_REDUMP` (No-Intro parser + `Tags.category`; only the DAT name selects it), `tags.CATEGORY_RULES` (Demos, Coverdiscs ->
  demo; Preproduction -> prototype), Redump tokens `(Taikenban)`, `(Tentou Taikenban)`, `(Tentou-you Taikenban)`, `(Tokubetsu Taikenban)`, `(Tentou-you Demo
  [Movie|Demonstration Movie])`, `(Trial ...)` -> demo (plus the No-Intro words), `library._select_redump`: game key = `tags.redump_game_key`
  (title + status + product tags; `(Disc N)`, dates, video, `Alt`, `Rerelease` excluded), editions = (regions, explicit languages); per game ONE
  edition (language rank, region priority, newest revision, fewest tags) with ALL its discs (newest revision of each disc); others superseded;
  `one_per_game` off keeps every edition. A complete multi-disc edition -> `ChosenSet` -> `m3u` playlist next to disc 1 (relative paths
  `../<disc 2 folder>/...`; labels off by default); incomplete -> `IncompleteSet(kept=True)` (reported, nothing moves). `Item.disc_total` from the DAT.
  Rule catalog for style `redump`: pre_release, prototype, demo, latest_only, one_per_game, languages, region_priority.
* **Convert** (`plan_convert` / `apply_conversions`, chdman only): temp folder, `createcd`, the new CHD is extracted again and every track compared
  with Redump (+ layout check), only then `create` journal record + move into `<name>/<name>.chd`, then the raw files move one by one to
  `_converted_originals/<rel>` (journalled; a failure rolls the moved ones back). Space check = 2 x raw size + 256 MB. Cancel / failure leave the raw set
  untouched and no temp folder. Undo = the standard `organiser.undo`.
* **Verify fully** (`verify_units`): decode every track of the `identified` CHDs (chdman or the pool), update the cache; mismatches are reported.

## Server / UI
`GET/POST /api/chdman` (detection incl. `steps` / `hint`, save `path` and `engine` auto|python), `POST /api/dc/verify` (job `verify`), status gains `redump`,
platforms gain `chd`; scan results: matched rows `level kind engine tracks`, games rows `level kind`, unmatched rows `reason kind`; organise / library rows add
`game level n_files files`; `convert/plan` adds `chdman`; `convert/apply` answers 409 + install hint without chdman. UI: Redump badge, level chips
(identified / verified / raw), cards, Dreamcast bar with **Verify fully**, chdman panel (found path or instructions, path override, engine), Redump
rules text, game-folder layout sketch, playlist option, convert text.

## Numbers
Real DAT (2026-06-14, 1516 games, categories Games 1183 / Demos 130 / Applications 84 / Coverdiscs 51 / Multimedia 24 / Preproduction 23 / Bonus Discs 14 /
Video 6 / Add-Ons 1; 151 discs carry `(Disc N)`: 104 two-disc, 24 three, 16 four, 7 lone "Disc 1"). All 1516 present, defaults (English, Europe > USA > World > Japan,
one per game): **kept 399 / excluded 904 (language 698, demo 183, pre-release 21, prototype 2) / superseded 213**, 12 playlists, 660 titles vanish (language 610,
demo 46, prototype 2, pre-release 2); no language filter: kept 933 / excluded 206 / superseded 377, 39 playlists; English + Japanese: 917 / 280 / 319; no
rule at all: 1133 kept / 383 superseded, 40 playlists, 8 discs with a missing sibling; one-per-game off: 596 kept / 16 superseded. Re-selecting the kept
set changes nothing. Unclassified (kept) by design: the categories Applications / Multimedia / Bonus Discs / Video / Add-Ons (129 discs) and the tokens
`Unl` (81), `Rerelease`, `Alt`, `Genteiban`, `Limited Edition`, `Omake Disc`, `Special Disk`, `Shenmue Passport`, `MilCD`, `Video ROM`, serials `610-xxxx`, dates, revisions.
Real CHDs (4 titles, `identified` = data tracks): JSR 35.0 s, Sonic Adventure 37.1 s, THPS2 31.9 s, Toy Commander 19.8 s (one core); whole real folder through the
server with 4 workers 44 s; rescan 0.2 s; **Verify fully** of all four (audio decoded in pure Python, 4 processes) 128 s; all 4 titles: every track crc32 / md5 / sha1 = Redump.

## Judgement calls / not verified
Raw sets and unmatched units are never touched by the library rules; Applications etc. stay; sidecar rule = names starting with the CHD stem (a longer sibling stem wins); an
incomplete multi-disc game stays in place. **Not verified here:** the real `chdman` (fake script only), a real Flycast / RetroArch load of the playlists, a real exFAT card,
CD (non-GD) CHDs with pregaps, a Flatpak MAME with and without filesystem access.


# AMENDMENT 12 - scratch space outside the library (chdman) and completing Amiga sets with other editions' disks

**Binding; additive (later wins over Amendments 6-11).** Stdlib only. New module `romorg/tempspace.py`, new tests `tests/test_tempspace.py`.

## A. Scratch space policy (`tempspace.py`; replaces the hidden `.romorg-chd-*` folder inside the ROM folder)
Every large temporary extraction (`chdman extractcd` while scanning, *Verify fully*, the verification of a converted CHD) uses ONE policy
and NEVER the user's ROM folder.
* **Required size** = the real track bytes of the CHD (`sum(Track.size)` = (frames - pad) x 2352, from `romorg/chd.py`) + 5 % + 64 MiB (`tempspace.required_bytes`).
* **Candidates in order** (`tempspace.choose(needed, avoid, accept) -> Plan(kind ram|disk|none, root, reason)`):
  (a) RAM: `ram_roots()` = `/dev/shm`, `$XDG_RUNTIME_DIR`, `/tmp` - each only when `/proc/mounts` says tmpfs/ramfs - used only if its free space
  (`statvfs`) >= required AND `MemAvailable` (`/proc/meminfo`) >= required + reserve (default 2 GiB; `ROMORG_TEMP_RESERVE_MB` or config
  `temp_ram_reserve_mb`; unknown MemAvailable = no RAM) so the machine is not pushed into swap; (b) disk: `$ROMORG_TEMP_DIR` / config `temp_dir`
  / `<data dir>/cache/tmp` when free space >= required, never inside a library folder (`avoid` = the scanned root) and never under a reserved
  folder name; (c) otherwise `NoTempSpace` (a `ChdmanError`): the caller falls back to the pure-Python reader and records why. A Flatpak chdman skips
  candidates it cannot see (`chdtool.acquire_workdir` passes an `accept` check).
* **Job folders**: `tempspace.acquire()` creates `<root>/romorg-job-<id>/` with a marker file `.romorg-temp-marker` (`{"app","pid","created"}`);
  `remove()` / `sweep_stale()` delete ONLY folders with that prefix AND marker (dead pid, or older than 24 h; our own active folders stay).
  Sweeps: server start (`App.sweep_chd_temp`: every candidate root + legacy `.romorg-chd-*` folders that carry a `pid` file inside the saved
  Dreamcast folder - a legacy folder without `pid` is no longer deleted), start of every scan / verify / convert, `finally:` of every decode (also
  cancel), and `atexit` (`cleanup_active`). Legacy `chdtool.make_workdir` is gone.
* **Reporting**: `tempspace.report()` -> `{"ram": n, "disk": n, "python": n, "last": {where, path, reason}, "text"}`; the job message says
  `decoding in RAM` / `decoding on disk: <path>` (progress label gets ` (in RAM)` / ` (on disk)`), `DcScanResult.temp`, `summary()["temp"]` +
  `["temp_text"]`, `verify_units(...)["temp"]`, `apply_conversions(...)["temp"]` (carried into the post-job summary by `App._carry_temp`).
  UI: a small muted line `#dc-temp-line` in the Dreamcast bar; no config field in the UI (config keys / env only).
* **Conversion output**: the NEW CHD is the intended output, not scratch: `chdman createcd` writes `<dst>.romorg.part` next to its destination
  (`dreamcast.PART_SUFFIX`), and it is renamed into place (`move_exclusive`) only after verification; a leftover `.part` of a crashed run is replaced;
  failure / cancel delete it. Space check: `raw_bytes` free next to the games (was 2 x). The verification extract of the new CHD follows the
  policy above; with no scratch space it is verified with the pure-Python reader (`hash_tracks_python`) instead of failing.
Tests: `test_tempspace.py` (policy with monkeypatched `mem_available` / `free_bytes` / `ram_roots` / `PROC_MOUNTS`: RAM when plenty, disk when RAM low or
tmpfs too small, none when nothing fits, reserve configurable, never inside the library / a reserved folder; marker + stale sweep incl. live-pid / unmarked /
foreign folders; fake chdman scans in RAM / on disk / python fallback with the library tree asserted unchanged DURING the extraction; cancel cleanup; verify;
conversion `.part` next to the destination).

## B. `borrow_other_editions` (Amiga Games-style DATs)
USER DECISION: a disk slot may be filled by a disk the user has (matched by checksum) that belongs to ANOTHER edition of the same title.
* **Profile**: `LibraryProfile.borrow_other_editions: bool = True` (persisted per platform; old profiles load True; `latest_only_profile()` False;
  `default_profile` True only for TOSEC platforms with `best_variant_dats`). Catalog entry `id borrow_editions`, `field borrow_other_editions`,
  kind option, tosec only (`rule_catalog`), `profile_info.available.borrow_editions`; `POST /api/library/profile` accepts the field. Scope: DATs in
  `best_variant_dats` (Games [ADF]) - never Workbench / Kickstart-Disks / Firmware.
* **Candidate rule** (`library._Borrow`, per DAT): pool key `(title, publisher, disk total)` (title = `Tags.title`, version / date removed) and disk number.
  A candidate must have the anchor's status tokens (`identity_key[5]`), a compatible chipset (`tags.chipset_compatible(anchor, disk)`: the anchor's platform
  classes - ECS counted as OCS, untagged = OCS - are a subset of the disk's, so OCS never takes AGA-only, AGA never takes untagged, `OCS-AGA` fits both)
  and dump flags that fit disk 1 (`m3u._compat`, via `m3u.pick_borrowed`: a different crack / `[t]` / `[h]` never fits, a bare disk fits `[cr X]`). Allowed
  differences: country, language, edition flags, version, year. NEVER borrowed: anything excluded by a quality rule or a keep-flag (bad dump, virus,
  pre-release, prototype, demo, faked, unreleased, modified when on, size problems, `flag_*`). Files excluded ONLY by the language filter form the
  reservoir too (`select` collects them as `lang_only`): the language filter must not exclude a borrowed disk.
* **Where**: `library._build_sets(..., borrow)` after `m3u.resolve_slots`: an anchored (disk 1 present) incomplete set whose disk total has NO complete set in the
  group gets EVERY missing slot filled or stays as it was (no best-effort). Per slot, tiers: 0 same edition (same identity + partition) but another
  version, 1 another edition in a selected / neutral language, 2 any other; inside a tier the newest `(version, date)` wins, ties by `_pick_slot`'s
  crack-compat order (`m3u.pick_borrowed`). A borrowed disk may be newer than disk 1. Disk 1 still defines the playlist name (recomputed from all chosen disks)
  and the ranking (`_Set.borrowed`; the best-variant order is unchanged). The group's own natural complete set always wins (no borrowing next to it).
* **Decisions**: `ChosenSet.borrowed: {slot: {"key","name","edition","differences": [country|language|edition|version|year],"text"}}`; after every group of the
  DAT is decided, each borrowed file whose decision is not `keep` becomes `Decision(KEEP, codes=("borrowed",), set_id, reason="borrowed as disk N of <set> (...)")`
  (an excluded-for-language, superseded or incomplete disk of the other edition is rescued; the other edition's remaining disks keep their classification).
  `Selection.borrow_summary()` -> `{"sets","disks","by_difference"}`. With the option off the old behaviour (and the old `elsewhere` text) is restored exactly;
  with it on the `elsewhere` text says the disk cannot be borrowed (chipset / dump flags / status).
* **Plan / API / UI**: `PlaylistSpec.notes` / `M3UOp.notes` (`disk 2 borrowed from the (DE) edition (Foo (1991)(Pub)(DE)(Disk 2 of 3))`; `untagged` when
  the other edition carries no tag; `another version of the same edition` for version / year only); kept borrowed files get that text as their op reason;
  `organiser.reason_counts` adds `borrowed_sets` / `borrowed_disks`; `/api/library/plan` adds `borrowed` `{sets, disks, by_difference}` and playlist rows `notes`.
  UI: rules-panel checkbox generated from the catalog, card *Sets completed with borrowed disks*, a `borrowed` chip + note under the playlist row.
* **Idempotence**: selection stays location independent; re-selecting only the kept files gives the same playlists (property test with random profiles incl. the
  option; real DAT checked for English / English + German / all languages, on and off). `BorrowTests` (test_library), `BorrowIntegrationTest` (test_integration:
  synthetic 3-disk set whose disk 2 is German only -> playlist complete, note, rebuild empty, undo restores).
* **Numbers** (real Games [ADF] DAT, every ROM present, `scratchpad/library-sim4.md`): English default: incomplete files 120 -> 79, incomplete sets 72 -> 44, playlists
  1244 -> 1258 (14 sets, 25 disks borrowed: 15 differ in language, 12 edition, 3 country, 1 year; most are translated `[tr en]` disk 1 + the Polish / German
  disks 2..n); English + German 157 -> 101 files; all languages 215 -> 113 files (26 sets, 58 disks). Changed decisions are confined to the 14 titles that got a borrowed
  set; every playlist of the old run is unchanged; ABC Monday Night Football and the AGA / OCS picks are identical. Guards that mattered (sets that would otherwise complete
  with a doubtful disk): chipset 3, dump-flag compatibility 9.
* **Judgment calls**: borrowing only when the group has no complete set of that disk total (a natural set is never replaced); an untagged disk is OCS, so an AGA set
  does not take it (conservative; the alternative would add a few sets); the same-edition-but-language-filtered disk (e.g. Polish disk 2 next to a `[tr en]` disk 1) is
  borrowed with `differences: ["language"]`; `Decision.codes == ("borrowed",)` also marks a disk that was already kept for its own set.


# AMENDMENT 13 - Sony PlayStation and PlayStation 2 (one shared disc-system engine)

**Binding; additive (later wins over Amendments 6-12).** Amiga / WHDLoad / the No-Intro consoles are unchanged. Stdlib only.

## Generalisation (`romorg/discsys.py`, thin config modules)
* `discsys.py` is the former `dreamcast.py` engine, parameterised by `DiscSystem(key, platform, dat_name, label, gd, iso, playlists,
  iso_convert, iso_convert_key)`; registry `SYSTEMS` (filled by `register()` in `dreamcast.py` = Dreamcast, `playstation.py` = `psx`, `ps2`),
  `system_for_dat(name)` (the engine finds the system from `DatFile.name`, so `scan(root, dat)`, `plan_tidy(result)`, `plan_library`,
  `plan_convert`, `apply_conversions`, `verify_units` keep their signatures), `system_for_platform`, `all_systems`, `iso_convert_mode`.
  `dreamcast.py` re-exports the engine (`from .discsys import *`) so every Amendment 11/12 name and test keeps working. `DcScanResult.system`,
  `summary()["system"]`; messages use `system.label` ("not part of a PlayStation 2 game"). The server calls `discsys` and adds `disc`
  (`DiscSystem.to_dict()` + `iso_mode`) to the platform rows and the convert page.
* Platforms `Sony PlayStation` (`Sony - PlayStation`, folder hint `psx`) and `Sony PlayStation 2` (`Sony - PlayStation 2`, `ps2`): source
  `redump`, layout `game_folder`, `convertible`, `latest/language/region_dats = (DAT,)`; own folder, config keys, library profile, DAT.
  `tags.REDUMP_DAT_NAMES` gains both names (the only thing that selects the Redump tag style). `redump.SYSTEMS` slugs `dc`, `psx`, `ps2`,
  `REDUMP_DATS` = three names; every Redump function takes a DAT name (default Dreamcast); per-DAT rows / manifest entries in the shared
  `data_dir()/redump`; `autoupdate`'s Redump source is unchanged (one HEAD per DAT per check, a zip only when newer; `ensure(platform)` scopes
  to that platform's DAT; the aggregated status ignores DATs that were never fetched; `latest` = the newest). Measured URLs:
  `http://redump.org/datfile/psx/` (zip 4.0 MB), `.../ps2/` (zip 1.3 MB).
* **Index** (`DcIndex`): a game's tracks = its `(Track N).bin` roms; else its single `<name>.bin` (PS2 CD games, single-track PS1 discs); else
  its single `<name>.iso` (`DcGame.iso`, PS2 DVD). Sizes tuple = the key (`(iso size,)` for an ISO game).

## chd.py / cdecc.py
* `cdecc.generate`: for a sector whose mode byte (`s[15]`) is 2 the four header bytes count as zeros for P/Q (ECMA-130, MAME `ecc_source_byte`);
  `generate_reference` likewise. Needed for MODE2_RAW (PlayStation, PS2 CDs): validated on the real Spider / FIFA / Dave Mirra CHDs (every
  track equals Redump).
* `_TYPES`: the cooked types (`MODE1` 2048, `MODE2_FORM1` 2048, `MODE2_FORM2` 2324, `MODE2` 2336) are stored at the START of the frame (offset 0;
  the previous offsets 16 / 24 were only right for cooked data inside a raw sector - no test and no real file used them). `MODE1` = a PS2 DVD
  ISO made by `chdman createcd`: 2048 bytes per frame, no pad, no subcode; hashing the whole track equals Redump's `.iso` (measured on all 7 PS2 CHDs).
* DVD CHDs (`chdman createdvd`): metadata tag `DVD ` and no CD tracks -> `Chd.is_dvd`, one synthetic `Track(type "DVD", frames = logical/2048)`,
  `iter_track` = the logical bytes (hunks decoded ~1 MiB at a time, prompt cancel); `raw_sha1` is the ISO's SHA-1. Codecs decoded: `zlib`, `lzma`,
  uncompressed, SELF references (all tested with the test writer). **Not decoded** (`ChdUnsupported`, `needs_chdman = True`, message "needs chdman:
  the CHD uses the 'zstd' compression ..."): `zstd`, `cdzs`, `huff`, `flac` data (the bundled Python 3.13 has no zstd). Header / metadata always read, so a
  createdvd CHD is identified even then.
* `hash_track` unchanged (single thread; a helper hashing thread was measured and gave nothing). No intra-track parallel decode (user decision);
  the per-file `chdpool` worker pool is unchanged and handles DVD CHDs.

## discsys behaviour
* **Identification**: DVD CHD without a cached hash: `meta[0] = {sha1: header raw SHA-1, claimed: True}`; `_rom_ok` compares sha1 only for a claimed track
  (candidates are chosen by size first), `_is_hashed` is false for it, so the level is `identified`, `via "header"`, nothing decoded, nothing cached.
  *Verify fully* (or chdman) decodes it, stores crc32 / md5 / sha1 and the next scan is `verified` from the cache. A CD CHD (createcd) is decoded and
  hashed; with no audio track `identified == verified` (all tracks hashed). A claimed hash that is wrong is only found by Verify fully ("track 1 does not
  match Redump"), after which the cache holds the real hashes and the unit is unmatched.
* **needs chdman**: a CHD whose codec the reader lacks and that chdman did not identify -> `DcUnit.needs_chdman`, unmatched entry `needs_chdman`, reason
  "cannot decode this CHD: needs chdman ...", plan status `skip` ("left in place") - it is NEVER moved to `_unmatched/`; `summary()["needs_chdman"]`.
* **Raw sets**: the sheet kinds are `.gdi` (Dreamcast only) > `.cue` > `.iso` (ISO systems); `sheet_track_files(iso) = [iso]` (never read as text);
  a loose `.iso` is hashed and matched like a track (level `raw`).
* **Progress**: `_Progress` message = `<what> (file i/n, NN MB/s, about T left)` (files counted = those that really decode; ETA after 3 s);
  `discsys.fmt_duration`. Cancel is prompt (per decoded chunk / killed workers).
* **Library**: unchanged rules (Amendment 11); `(Disc A)` / `(Disc B)` are discs 1 / 2 (`tags._DISC_RE`, `library._DISC_TOKEN_RE`; two PS1 games would
  otherwise collapse to one). `plan_library`: `DiscSystem.playlists` False (PS2) -> no `PlaylistSpec`, no stale-playlist handling, `plan.playlists == []`.
* **Convert**: `DcConvertOp.mode` `createcd` (cue / gdi sets) or `createdvd` (a single `.iso` of a system with `iso_convert`, PS2: default `createdvd`,
  config `ps2_iso_chd` = `dvd` | `cd`); `chdtool.create_dvd` / `extract_dvd` (`chdman createdvd` / `extractdvd`); the new CHD is verified by decoding
  (chdman `extractdvd` in scratch space, else the built-in reader) against the Redump ISO before anything moves; `/api/convert/plan` rows carry `mode`.
  PCSX2 reading both createcd and createdvd CHDs is an ASSUMPTION (not verifiable here).

## UI
`platform.disc` drives the texts: raw kinds (`.cue / .iso`), no playlist promise / labels checkbox for PS2, createcd / createdvd note in the Convert step,
engine line mentions CHDs that need chdman, speed note (30-45 MB/s per CD/DVD, FLAC 1 MB/s), progress line with file i/n, MB/s and ETA. The systems table,
DAT status, folders (saved immediately), chips, Verify fully, chdman panel, rules panel (catalog) and Build library preview are the Dreamcast ones.

## Numbers (real DATs 2026-06-15, every game present, defaults)
* **PlayStation** (10,914 discs; Games 8556 / Demos 1372 / Coverdiscs 271 / Applications 191 / Educational 187 / Preproduction 187 / Bonus Discs 59 /
  Multimedia 52 / Add-Ons 36 / Video 2 / Audio 1; 367 `(Rev N)`, 1488 `(Disc N)` entries): kept 2270 / excluded 7512 (language 5676, demo 1648, pre-release 178,
  prototype 10) / superseded 1132; 72 playlists, 18 incomplete multi-disc games; 4798 titles vanish (language 4359, demo 424, prototype 8, pre-release 7);
  no language filter: 6423 kept, 287 playlists; one-per-game off: 3256; no rules: 10525 kept.
* **PlayStation 2** (11,774: 8606 `.iso`, 3168 CD; Games 9477 / Demos 1015 / Applications 485 / Coverdiscs 439 / Preproduction 286 / Bonus Discs 37 / Multimedia 18 /
  Add-Ons 10 / Educational 4 / Video 3; largest ISO 8,539,963,392 bytes): kept 3188 / excluded 6721 (language 4978, demo 1457, pre-release 271, prototype 15) / superseded
  1865; no playlists (11 complete + 10 incomplete multi-disc sets stay together); 4332 titles vanish; no language filter: 6637 kept; one-per-game off: 4893; no rules 11475.
  Re-selecting the kept set changes nothing (both).
* Classified tokens / categories: Demos, Coverdiscs -> demo; Preproduction -> prototype (+ `Beta` pre-release, `Proto`, `Sample`, `Taikenban`, `Trial Edition`);
  deliberately kept: Applications, Educational, Bonus Discs, Multimedia, Add-Ons, Video, Audio, `Unl`, `Rerelease`, `Alt`, budget lines (`PlayStation the Best`,
  `Greatest Hits`, `Platinum`), special editions. Unclassified by design: name-only demo-ish discs in Applications / Games categories (e.g. `Omega Boost Trial Version`
  inside `Play-Pre Vol. 17 (Disc 2)`, `GameShark Sampler`, `Karat ... Taikenban` utilities, `Layer 0/1` betas which are excluded anyway by their Beta tag).

## Real files (read-only validation, built-in reader, one core, no cache)
FIFA 98 (USA) 21.4 s (25 MB/s), Spider 10.8 s (40 MB/s) - both `verified` = Redump; PS2: Bully (USA) 136 s, Dave Mirra Freestyle BMX 2 (USA) 26 s (MODE2_RAW),
God of War II (USA) 194 s (8.5 GB, 44 MB/s), Gran Turismo 4 -> *Gran Turismo 4 (USA, Canada) (v1.01)* 118 s, GTA Vice City -> *(USA, Canada) (v3.00)* 145 s, Mat Hoffman's
Pro BMX 2 (USA) 141 s, Tony Hawk's Pro Skater 4 -> *(USA) (v1.02)* 90 s; all `verified`. Three of the seven user names differ from Redump's (region list / version tag) and are renamed by Organise.

## Judgement calls / not verified
Budget re-releases and special editions are separate games (never merged); a trial disc inside a Games entry stays; `needs chdman` CHDs are skipped, not moved. **Not verified
here:** the real chdman (createcd, createdvd, extractdvd: only a fake script), PCSX2 / DuckStation / RetroArch loading the CHDs or playlists, CHDs written by a real
`chdman createdvd` (only the test writer's; the codecs there are lzma / zlib / none and the header SHA-1 claim follows the documented format), zstd decoding, exFAT.


---

# AMENDMENT 14 - the fastest SUPPORTED CHD engine (parallel scheduler, native FLAC, bundled chdman, GD-ROM safety)

**Binding; additive (later wins over Amendments 6-13).** "Supported" = only what emulators and the shipped AppImage support: standard CHD codecs
(LZMA / zlib / FLAC), never zstd / `cdzs` written by us; no compiler, no Rust, nothing installed system-wide. App code stays stdlib-only; shared
libraries are only *loaded* (ctypes).

## Modules
* `chdsched.py` (replaces `chdpool.py`): ONE work queue over all tracks of all CHDs. A track is split by `chd.plan_chunks` into ~4 MB (extracted bytes)
  ranges on hunk / ECC-group (16 hunks) boundaries; N `python -m romorg.chdworker` processes (default one per CPU thread, limited by `MemAvailable`
  at ~80 MB per worker; config `chd_workers`, env `ROMORG_CHD_WORKERS`, 1 = sequential in-process = the old path) decode ranges
  (`Chd.read_track_range`) and send the bytes back through their stdout pipe (1 MiB pipe buffer; measured cheaper and safer than shared memory:
  no resource tracker, nothing left behind after a kill). Workers keep up to 4 opened CHDs with their parsed hunk maps (compact `array`s).
  FIFO dispatch in global request order + per-track ordered hasher (`multihash`: crc32 + md5 + sha1, the digests of a chunk in parallel threads)
  + back-pressure (`budget` <= 256 MB of chunks not yet hashed, at most 2 requests in flight per worker, chunk size shrinks to fit the budget).
  `Scheduler.hash_tracks(info, indexes, progress, cancel)` is thread-safe: `discsys` identifies several CHDs at once (threads), their chunks share the
  workers; the early-reject strategy of `match_chd` (candidates by size, data tracks smallest first, audio last) is unchanged and runs on top.
  Failure model: a dead worker's chunks are retried (<= 2 tries) on a respawned worker (<= 3 respawns), otherwise `PoolError` and the caller
  (`discsys.hash_tracks_python`) hashes the rest in-process; `Cancelled` on cancel; `close()` SIGKILLs the process groups and reaps them;
  an unreadable file raises the reader's `ChdError` / `ChdUnsupported(needs_chdman)` without breaking the pool. Property tests: random chunk sizes
  x layouts x codecs (CD raw / cooked / MODE2 / audio / DVD) == sequential `hash_track`.
* `flacnative.py`: ctypes binding to libFLAC's stream decoder, fed like libchdr (synthesised `fLaC` + STREAMINFO, 44.1 kHz / 2 ch / 16 bit, frames of
  the hunk, `process_single` until the hunk's samples, `get_decode_position` = start of the zlib subcode). Library order: `$ROMORG_LIBFLAC`, bundled
  `tools/lib` (libogg preloaded RTLD_GLOBAL), system `libFLAC.so.*`; else the pure-Python `flacdec` (same results). Bit-identical to `flacdec` and to Redump;
  unlike `flacdec` it checks the frame CRCs (damaged audio -> `ChdError`). One decoder per thread.
* `multihash.py` (parallel digests, `hash_file` with a prefetching reader thread), `bundle.py` (where the AppImage keeps `tools/`, `licenses/`),
  `selfcheck.py` (`--self-check`), `tools/bench_chd.py` (not in the test suite).
* `chd.py`: map parsing 2.7x faster (`binascii.crc_hqx`, one 64-bit window per field; 6 GB image 3.6 -> 1.4 s), compact map arrays, cdfl through `flacnative`.

## Engine policy (`chd_engine`: auto | python | chdman)
`auto` = built-in reader + scheduler first; chdman only (a) to *create* CHDs (multi-core), (b) as the fallback when the reader raises `ChdUnsupported`
with `needs_chdman` (extract through `tempspace` as before). `python` never uses chdman; `chdman` forces extraction first. `DcScanResult.engine` is the engine
that really decoded (`python` | `chdman` | `mixed`), `engine_info` / `summary()["engine_info"|"engine_text"]` carry MB/s, processes, native FLAC; Verify fully and
Convert return `engine_text` / `verify_text`; the UI bar shows "Last decode: Built-in reader (8 processes, native FLAC): 395 MB in 3 s, 124 MB/s".

## Convert safety
* After `createcd` / `createdvd` the CHD kind is asserted (`assert_new_chd`): a GD-ROM system must get CHGD, not CHT2 (the track hashes cannot tell them apart).
* A GD system with only a `.cue` is converted only through a generated `.gdi` (`gdi_from_cue`): needs the Redump `REM SINGLE-DENSITY AREA` / `REM HIGH-DENSITY AREA`
  markers; track 1 at LBA 0, SD tracks back to back, first HD track at 45000, then back to back, type 0 audio / 4 data, 2352 bytes. The `.gdi` and symlinks to the track
  files are written to a scratch folder (the library is only read). Validated: 245 / 245 sidecar `.gdi` files of the real library equal the generated rows (sizes from the CHD),
  and a real-chdman round trip on Jet Set Radio (extractcd -> Redump-named set + marker cue -> `createcd` from the real gdi and from the generated gdi): both
  CHDs have the header SHA-1 `465686c8...` of the original. Otherwise the op is `skip` "needs a .gdi: ...".
* The new CHD is verified independently of the creator: our reader through the scheduler (chdman extract only as fallback / forced engine).

## Packaging
`build_appimage.sh` downloads the pinned Arch packages (mame-tools 0.289-1, libutf8proc 2.11.3-1, flac 1.5.0-1, libogg 1.3.6-1; sha256 verified, fail on mismatch, cached in
`packaging/.cache`), bundles `tools/chdman`, `tools/lib/{libutf8proc.so.3,libFLAC.so.14,libogg.so.0}`, `licenses/` (+ `docs/THIRD_PARTY.md`), runs `--self-check` (bundled
libFLAC loads and decodes, scheduler runs, chdman starts) and `smoke_test.sh` repeats it inside the AppImage. `BUNDLE_TOOLS=0` skips it. `chdtool` detection: configured path -> bundled
(`LD_LIBRARY_PATH` = bundled lib dir first) -> PATH -> Flatpak MAME -> folders; a bundled chdman that cannot start gives the note "bundled chdman could not start: missing libSDL2 ..." and the next one is used.

## Loose files / archives (measured on the Deck, warm cache)
* 2 GiB file crc32 + sha1: plain loop 606-850 MB/s -> prefetch thread + parallel digests 1600 MB/s (`scanner.hash_file`, `discsys` raw sets); with md5 as well 401 -> 625 MB/s.
* 3000 files of ~120 KB: sequential 0.49 s -> 4-thread pool 0.32 s (`scanner.scan` hashes cache misses and lists 7z / rar archives ahead in a pool of 4, `ROMORG_SCAN_THREADS`;
  results consumed in file order). zip: central-directory CRC, unchanged. 7z listings run in the same pool (not separately benchmarked: no 7z binary inside the sandbox).

## Numbers (Steam Deck, 8 threads, real files, read-only; before = one process, pure-Python FLAC = the previous code path)
| Disc | tracks bytes | before (1 core) | chdman extract+hash | 1 core + native FLAC | **scheduler, 8 procs** | peak RSS |
|---|---|---|---|---|---|---|
| Jet Set Radio (DC, data heavy) | 1134 MB | 38.0 s | 46.2 s | 35.5 s | **8.2 s (138 MB/s)** | 435 MB |
| Toy Commander (DC, 390 MB FLAC) | 1148 MB | 371.9 s | 32.8 s | 26.2 s | **6.5 s (176 MB/s)** | 470 MB |
| 4x4 Evo (DC, audio heavy) | 1134 MB | 210.4 s | 19.7 s | 18.9 s | **5.1 s (223 MB/s)** | 455 MB |
| FIFA 98 (PSX) | 511 MB | 20.8 s | 26.0 s | 20.5 s | **4.6 s (112 MB/s)** | 420 MB |
| Bully (PS2, 2.4 GB chd) | 4422 MB | 127.4 s | 170.4 s | 113.1 s | **23.6 s (187 MB/s)** | 834 MB |
| God of War II (PS2, 6.6 GB chd) | 8138 MB | 185.3 s | n/a (extract > 8 GB scratch cap) | 188.1 s | **40.2 s (202 MB/s)** | 1003 MB |
All hashes (size, crc32, md5, sha1 of every track) are identical between all strategies. Folders: PS2 (7 CHDs, 30 GB decoded) old 4 file-level processes 309 s -> 158-192 s;
Dreamcast sample of 32 titles (15.5 GB of CHD, data tracks): old 625 s -> new 220 s, same matches and levels; the whole Dreamcast folder (now 423 CHDs) 47 min new (cold scan,
no cache, 18.5 CPU-hours). Native FLAC: 80-83 MB/s of PCM per core on loud audio (libFLAC ~60 %, ctypes callbacks + interleave the rest; quiet audio is faster) - the 100 MB/s per-core target was NOT met
on high-entropy audio; the whole-disc effect is what matters (Toy Commander 372 -> 26 s on one core). Parallel scaling is 3-3.5x on 4 cores / 8 threads: LZMA (36 %) and the ECC rebuild
(47 %) are CPU / memory bound.

## Judgement calls / not verified
Pipes instead of shared memory; workers are `nice` +5; the scheduler never reads more than the chunks it needs (no whole-file buffering). **Not verified:** Flycast / PCSX2 loading CHDs made by
this path, exFAT, a bundled chdman on non-SteamOS libraries (it needs libSDL2 / libz / libzstd / libstdc++ from the system; the fallback is reported), the 7z listing speed-up, the generated-gdi
path on discs whose cue lacks markers (refused by design), real-file results for the old code on the whole Dreamcast folder (extrapolated from the 32-title sample).


# AMENDMENT 15 - system cards + per-system tabs UI, last-scan records, checksum view (later wins)

## UI structure (replaces the step layout; the top step bar and per-system step numbers are gone)
* Global header (every view): title / home link, updates line + **Check for updates**, **Quit**, one **job bar** (`#job-bar`: the running job of ANY kind with its system name, progress, Cancel; a finished job hides itself after 8 s).
* Hash routes, no server support needed (the server still only serves `/` and `/static/*`): `#/` = home; `#/system/<slug>/<tab>[?view=&have=&dat=]`, `<tab>` = `overview | library | browse | tools`. `slug` comes from `/api/platforms` (`slug`, `server._slug`: lower-case, runs of non-alphanumerics -> `-`); an unknown slug returns to `#/`, `tools` on a system without tools falls back to `overview`. Tab buttons change the hash (Back / Forward / reload work); changing the Browse list / filters uses `history.replaceState`.
* Home: one card per system from `/api/platforms`, grouped by what the system IS (disc layout -> *Disc systems*, No-Intro -> *Cartridge consoles*, else *Computers*); primary button = Set folder (no folder) -> Scan (no record, or the record is of another folder) -> Build library (opens `#/system/<slug>/library`). Live progress of a running job is drawn on the card of `job.platform`.
* System page: Overview (folder field, DAT status + collapsible DAT table, Scan, summary cards that link to Browse), Library (rules panel collapsed to a one-line summary built from `/api/library/profile`: exclusions on, languages, `ranking_short` or the top regions, keep-flags that are off, options on; Preview / Build / Undo; *Advanced* keeps Organise + M3U), Browse (built lazily: `renderBrowse` only runs when the tab is open; `browseDirty` marks a rebuild), Tools (`tool-convert` if `convertible`, `tool-verify` if game-folder layout, `tool-kickstart` if `has_kickstart`; Tools tab hidden if none). Scan-dependent tabs show *Scan first* + button without an in-memory scan. The rule counts (`/api/library/plan?limit=1`) are fetched only while the Library tab is open.
* Touch targets >= 44 px, `:focus-visible` rings, tablists support arrow keys, the page never scrolls horizontally (wide checksum tables scroll inside their own box).

## API additions
* `GET /api/platforms` rows gain `slug` and `last_scan` (the record below or `null`).
* Jobs (`/api/job`, `POST` answers) gain `platform` (the system the job works on).
* `GET /api/library/profile` gains `ranking_short` (`"CD32 over AGA over OCS"` or `""`).
* `config.json` key `scan_records`: `{platform: {at, folder, count_by, total, have, missing, pct, matched_files, unmatched_files, duplicates, errors, dats, [chd_files, identified, verified, raw]}}`, written by `App._record_scan` after EVERY completed scan (also the re-scans after organise / library / convert / verify) through `paths.update_config` (same lock as every other writer); `server.scan_record()` builds it; never lists or per-file data.
* `GET /api/scan/results` rows of `matched | missing | unmatched | games` carry `id` (index in the unfiltered list); with `checksums=1` each row of the PAGE also carries `checksums` (built per page from the scan in memory - cached rows are not touched, so pages stay fast). `GET /api/scan/checksums?kind=&id=` returns the same payload for one row (400 for other kinds, 404 for an unknown id).

## Checksum payload (`App._checksums`; hashes are lower-case hex, `null` = not known, nothing is computed or guessed)
```
{"kind": "file" | "disc" | "missing" | "unmatched" | "disc_unmatched" | "none",
 "source": "tosec|nointro|whdload|redump", "dat_name": "...",
 "dat":   [{"name","size","crc32","md5","sha1"}, ...],            // the DAT roms (a set's alternates; the primary rom of a match)
 "local": [{"file","member","archive","size",
            "raw": {"crc32","md5","sha1"},                         // what the scan knows of the file as stored (md5 is always null: scans hash CRC32 + SHA-1; an archive member has CRC32 only)
            "via": "raw|headerless|byteswapped", "via_text": "...",
            "normalised": null | {"crc32","md5","sha1","size"},    // the hashes of the normalised content that matched (alt matches)
            "equal": {"crc32","md5","sha1"}}],                     // true/false when both sides know the hash, else null; computed on `normalised` when present
 "also_named": n, "more_files": n,
 "disks": {"name","total","complete","disks":[{"number","missing", "dat", "local", "this"}]}}   // multi-disk sets (m3u.group_disk_sets); missing disks have no hashes
disc kinds: {"kind":"disc","game","level","disc_kind",
 "tracks":[{"number","type","size","dat":{name,size,crc32,md5,sha1},"local":{crc32,md5,sha1}|null,
            "state":"hashed|length|header|none","equal":{...}}],      // length = audio identified by length only; header = a DVD CHD's header SHA-1 (not decoded); none = nobody has the game
 "local":[{"file","size","chd_sha1","kind"}]}
```
Sources: the `Match` / `Entry` objects of the scan (`ScanState.sources`, `match_index`, `disk_sets` are built lazily), the DAT `sets()` and, for discs, `DcScanResult.index` / `units`.

## Verified
Headless Brave (DevTools protocol) at 1280x800 and 900x800 against a temp data dir with copies of the real DATs and synthetic ROMs (see the final report of the change); tests: `ChecksumServerTests`, `UiStructureTests` (tests/test_server.py), the per-track disc test in tests/test_dc_server.py.


# AMENDMENT 16 - unmatched files always move; drag-and-drop region priority (later wins)

## A. "Move unmatched files into `_unmatched/`" is gone
* The option is removed everywhere: the Build library checkbox (`lib-move-unmatched`), the Advanced organise checkbox
  (`organise-move-unmatched`), their handlers / request payloads, the `move_unmatched` parameter of `organiser.plan_renames`,
  `organiser.plan_library`, `organiser._plan_core`, `organiser._Planner`, `discsys.plan_units|plan_tidy|plan_library` and the
  `False` branches (the "left in place (moving unmatched files is off)" skip rows). Behaviour is the former default (True).
  This supersedes every mention of `move_unmatched` / "Move unmatched" in the amendments above.
* Server: `POST /api/organise/plan|apply`, `POST /api/library/plan|apply|vanished` and `GET /api/scan/results?kind=rename` still
  ACCEPT a `move_unmatched` field from a stale client and ignore it (no 400). The plan caches are keyed by `latest_only`
  (`ScanState.rename_plans`) and `(savedisk, labels)` (`library_plans`); the plan response no longer carries `move_unmatched`.
* The protections that make always-moving safe are unchanged: the over-broad folder guard (`/`, home, drive roots, the app data
  dir), frontend media / save dirs and keep-files left alone, symlinks, hidden files and user M3Us untouched, reserved folders,
  folders of DATs that are not installed, preview first (an "Unmatched -> `_unmatched/`" card and, in the confirmation, "N unmatched
  file(s) -> `_unmatched/`"), the extra warnings dialog and the undo log.
* Behaviour change in one corner: a stray file squatting on a game's target name is now moved to `_unmatched/` first, so the game can
  take its place in the same apply (before, with the option off, that case was a `conflict`). Nothing is overwritten (test
  `test_never_overwrites_an_existing_target`).

## B. Region priority list = reusable `sortableList` component (static/app.js)
* Pointer Events, no HTML5 drag-and-drop: a `.drag-handle` (44 px wide, `touch-action: none` on the handle ONLY, so a swipe on the rest
  of the row still scrolls the list) starts the drag on `pointerdown`; `pointermove|up|cancel` are listened to on `window`
  (+ `setPointerCapture` on the list, because the row that holds the handle is re-inserted in the DOM while it moves). A fixed-position
  ghost follows the pointer, the real row stays in the list as the dashed placeholder (= the drop indicator) and moves live; the ghost
  shows the position it would get. `requestAnimationFrame` auto-scroll (up to 18 px / frame) when the pointer is within 64 px of the
  list's top / bottom edge. Escape or `pointercancel` restores the order.
* Keyboard: roving tab stop on the rows (arrows move focus; Home / End), Space / Enter picks up, Up / Down / Home / End move,
  Space / Enter / Tab drop, Escape cancels; an `aria-live` region (`role=status`) announces pick-up, each position, drop, cancel.
  Each row also has **Top** (new), up and down buttons (44 px).
* The list is the FULL region order from the server (`profile_info.regions`, never a hard-coded list). The filter ("Find a region...")
  only HIDES rows (matches highlighted; the divider is hidden while filtering); a drop puts the row before the next visible row (or after
  the last visible one), hidden rows keep their relative order, so the saved list is always a permutation of all regions.
* Fixed list height `min(360px, 60vh)` (no layout shift when filtering; the "no match" line lives inside the list). The first 4 rows keep
  the highlight and a "Prioritised / Everything else" divider follows row 4.
* Persistence: `commitRegionOrder` updates `info.regions` at once (optimistic), then ONE debounced (250 ms, coalescing rapid button
  clicks) `POST /api/library/profile {platform, region_priority: [full list]}`; saves are serialised. On failure the list is rolled back
  to the last confirmed order and a toast `Could not save the region priority: ...` is shown. The rules panel is NOT re-rendered after a
  successful save (so scroll position and focus stay); the summary line is refreshed in place (`renderRulesSummary`).
* The language "Preferred first" pills keep their ▲ / ▼ buttons (pills wrap on several lines; a handle was not cheap there).

## Verified
Headless Brave over DevTools (injected mouse AND touch input, which produce real `pointerType` mouse / touch events), 1280x800 and 900x800:
bottom-to-top drag with auto-scroll, mid-drag Escape, keyboard pick-up / move / drop / cancel, Top button, filter-then-drag,
failed-save rollback, one POST per drop, saved order after a server restart, touch swipe on a row scrolls the list. Not verified:
real touchscreen hardware, a physical gamepad through Steam Input.



# AMENDMENT 17 - library totals on the Overview, and the "Recalculate preview" affordance (later wins)

## A. Library totals ("With your library rules: have N of M games")
Code: `romorg/totals.py` (new), `library.game_key`, `server.App.library_totals`, `static/*`.

* **Meaning.** *M* (the TARGET) = the games the system's current `LibraryProfile` keeps when EVERY rom of the platform's DATs is present
  (`library.select` over the whole DAT). *N* = the games of that target the user owns in at least one passing version = `library.select`
  over the user's matched files (already in memory; no file is touched), counted per GAME. A multi-disk game needs a complete set
  (`Decision.action == incomplete`, or a Redump `keep` with `Decision.missing`, is not "have"). Applies to every system (Amiga Games +
  Workbench / Kickstart-Disks / Firmware, WHDLoad, the 4 No-Intro consoles, the 3 Redump disc systems).
* **Game identity** = `library.game_key(item)` = `(dat, tags.game_key(tags))` - exactly how `select` groups variants: TOSEC / WHDLoad
  `tags.identity_key` (title, country, publisher, edition and status tags; language, chipset, version, disk are NOT part of it), No-Intro /
  Redump `tags.game_key` (title, status, product tags; regions, languages, versions and DISC NUMBERS are not part of it, so a 2-disc
  Redump game is ONE game). A disk BORROWED from another edition (`Decision.codes` has `borrowed` and `set_id` is the borrowing set's id)
  is no ownership of its own game and is skipped in both selections.
* **Target items** (`totals.target_items`): one item per ROM (TOSEC), per set (No-Intro / WHDLoad: the first of its alternate roms), per
  disc (Redump, `DcIndex.games`, `disc_total` from `disc_totals()`), `form == ""`. **User items** (`totals.user_items`): `organiser.matched_units`
  de-duplicated by `(dat, rom name, form)` (copies of one rom have identical decisions), or one item per CHD game for the disc systems
  (as `discsys.plan_units`).
* **Numbers** (`totals.combine`): `have_games` = user games kept AND in the target; `not_preferred` = of those, games whose kept rom names do
  not intersect the target's kept names for that game (one-per-game: "the version you would keep differs from the one the rules pick for the
  full DAT"; with several kept versions per game: none of yours is among the target's); `owned_but_excluded` = owned games all of whose
  variants are excluded (beta / bad dump / other language ...; their identity is usually not even in the target); `owned_incomplete` =
  owned multi-disk games without a complete set; `owned_outside_target` = kept games that are no target game (changed DAT); `owned_games` =
  distinct game keys of the user's files, so `have + owned_but_excluded + owned_incomplete + owned_outside_target == owned_games` and
  `have <= target`. `target_incomplete` = games the DAT itself only has in part (cannot ever be complete: not in M); `by_dat` = `{dat: {target, have}}`.
* **Background computation** (`totals.TotalsManager`): ONE daemon worker thread, never inside a request or a scan job. `request()` registers what is
  wanted `(platform, profile signature, DAT signature, scan serial)` and returns at once. Keys: target = `(sha1(profile.to_dict()), sha1(DAT names +
  paths + mtimes))`, user part = `(ScanState.serial, profile signature, DAT signature)`. Results are cached in memory per platform; the same key is
  never computed twice (polling is free); a result whose key was superseded while it ran is dropped (`discarded`); a failure is cached and reported
  (`error`), not retried in a loop. While new numbers are computed the previous ones stay in the answer with `stale: true`. The worker holds the
  `dat_lock` only through `App._platform_dats`. `ScanState.serial` (new field, also `scan.id` in `GET /api/status`) changes with every scan / re-scan.
* **Persistence**: after every target computation `config.json["library_totals"][platform] = {target_games, target_incomplete, by_dat, profile_signature,
  dats_signature, computed_at}` (counts only, written through `paths.update_config`). When it matches the current rules + DATs and no scan is loaded,
  the answer is instant and nothing is computed.
* **Endpoint** `GET /api/library/totals?platform=` (default platform when omitted; 400 for an unknown one):
  `{"platform","available","scanned","calculating","stale","error","computed_at","profile_signature","target_games","target_incomplete","have_games","missing_games",
  "percent","not_preferred","owned_but_excluded","owned_incomplete","owned_outside_target","owned_games","by_dat"}`; `available: false` = no DAT of the system
  is installed; `have_games` & co. are `null` until a scan of THAT platform is loaded and computed. Invalidation is by key (a rule change, a DAT update, a
  new scan).
* **Measured** (real DATs, default profiles, this machine): Amiga (all 4 DATs) M = 3,656 games in 5.0 s (Games [ADF] 3,591; Workbench 10; Kickstart-Disks 5; Firmware 50; DAT
  load 0.8 s); WHDLoad 2,763 in 0.3 s; N64 402 / NES 2,650 / GBA 1,438 / SNES 1,110 (0.1-0.6 s); Dreamcast 381 games (399 kept discs), PlayStation 2,150
  games (2,270 kept discs), PlayStation 2 3,167 games (3,188 kept discs) in 0.2 / 1.4 / 1.6 s. Amiga Games with borrowing OFF = 3,578 (the 2026-10-02
  simulation); borrowing completes 13 more games. 31 Amiga games, 18 PlayStation and 10 PlayStation 2 games are only partly in their DAT (`target_incomplete`).

## B. Previews: Recalculate, out of date, Apply blocked
* Every Preview button (`lib-plan-btn`, `plan-btn`, `convert-plan-btn`, `m3u-plan-btn`, `kick-plan-btn`) is driven by `Previews` (app.js): states
  `none | busy | ready | stale | error`. Label: first press = its own label; afterwards **Recalculate preview** + refresh icon, status line
  `Calculated HH:MM · N files` (`role=status`, `aria-live=polite`) and the hint "Recalculates with your current rules and folder contents"; busy =
  spinner + "Calculating..." (button disabled, `aria-busy`); error = "Try again" + the message.
* **Stale** (the preview stays on screen): a banner (`.pv-banner`, with its own Recalculate button) "Rules changed since this preview" /
  "Options changed ..." / "The folder was scanned again since this preview", dimmed output (`.pv-stale`), the Preview button highlighted
  (`btn-primary`, ring), and the matching Apply button disabled (`data-stale="1"` is honoured by `Jobs.setRunning` next to `data-blocked` /
  `data-busy`) - also while the recalculation runs or failed. Triggers: `profileChanged` and the "latest only" box (library, organise, convert), the
  labels / save-disk boxes (library, M3U playlists), every scan (`applyScan` -> `Previews.onScan`: same system = stale, other system / no scan = dropped).
  A reload of the table (paging / filters) returns the CURRENT plan and so makes it fresh again. Jobs that finish (build, undo, convert, playlists)
  re-scan, mark the old preview stale and re-run it when it was open.
* **Server**: `POST /api/library|organise|convert|m3u/plan` accept `refresh: true` (the Recalculate button) and drop the cached plans first.
  `POST /api/library/plan` returns `plan_id` (`<scan serial>.<profile signature>.<savedisk><labels>`) and `files`; `POST /api/library/apply` accepts the
  `plan_id` the client confirmed and answers 409 `stale_plan` when it no longer matches (the UI sends it; absent = old behaviour). Independently of that,
  Apply ALWAYS runs on a plan built for the CURRENT saved profile: `_library_plan` compares the cached plan's `profile` with the saved one and rebuilds
  on any difference (before: only profile saves through the API cleared the cache). The plan caches stay keyed by `(savedisk, labels)` and are dropped with
  the `ScanState` on every re-scan.

## Verified
Headless Brave over DevTools at 1280x800 and 900x800 (temp `ROMORG_DATA_DIR` with copies of the real DATs, 62 synthetic ADFs patched into the DAT copy, one real
Dreamcast CHD): totals unscanned -> scanned -> after a rule change (stale numbers + "recalculating..." -> new numbers), upgrade / excluded / incomplete lines, "Scan to see
how many you have" (N64), Preview -> Calculating... -> Recalculate preview, stale banner + disabled Build after a rule / option / scan change, fresh plan after
recalculating, the error state and Try again, a Build library run, no console errors. Not verified: real touch hardware, a gamepad, screen readers (the live regions
are in place).


# AMENDMENT 18 - game ratings and the rating filter of the library rules (LaunchBox Games Database; later wins)

Code: `romorg/ratings.py` (new: download, streaming parser, sqlite index, matching), `library.py` (profile fields, `RatingContext`,
`apply_ratings`, `target_cutoff`, `rating_coverage`, `rom_title`, catalog group, vanish hints), `totals.py`, `autoupdate.py`
(`ratings` source), `paths.ratings_dir()`, `server.py`, `static/*`, `tests/test_ratings.py`. Python stdlib only.

## A. Source and compliance
`https://gamesdb.launchbox-app.com/Metadata.zip` (about 108 MB, rebuilt daily; `HEAD` gives `Last-Modified` / `ETag`; free, no key; `Metadata.xml` inside
is about 512 MB, plus `Mame.xml`, `Files.xml`, `Platforms.xml` that are never read). `<Game>`: `Name`, `Platform`, `ReleaseYear` / `ReleaseDate`,
`DatabaseID`, `CommunityRating` (0-5 float; absent or 0 = unrated), `CommunityRatingCount`, ...; `<GameAlternateName>`: `AlternateName`, `DatabaseID`,
`Region` (no platform of its own: the game's DatabaseID links it; 70,970 of them on 2026-10-04). Platform strings used (`ratings.LB_PLATFORMS`):
`Commodore Amiga` (TOSEC Amiga AND WHDLoad), `Nintendo Game Boy Advance`, `Nintendo 64`, `Nintendo Entertainment System`,
`Super Nintendo Entertainment System`, `Sega Dreamcast`, `Sony Playstation`, `Sony Playstation 2`. A system without an entry has no Ratings group.
**Terms**: none could be found - the Games Database pages and the LaunchBox help / forum pages that were read state no licence, terms of use or
attribution requirement for the download (see `docs/THIRD_PARTY.md`: credit line "Ratings: LaunchBox Games Database community ratings", shown in the
rules panel, README, THIRD_PARTY; no legal conclusion is drawn).

## B. Profile fields (`library.LibraryProfile`, persisted per platform like the others; old profiles load with the defaults)
| field | type / default | meaning |
|---|---|---|
| `min_rating` | `float` 0.1-10 or `None` (default None = off) | rated games below it are excluded (`0` / out of range = off; rounded to 0.1) |
| `top_n` | `int` >= 1 or `None` (None = off) | keep the N best-rated games |
| `min_votes` | `int` >= 1, default 5 | fewer votes = the game counts as UNRATED |
| `keep_unrated` | `bool`, default False | with a filter set, games without a usable rating are kept IN ADDITION |
| `rank_scope` | `"dat"` (default) \| `"owned"` | rank / top N against the whole filtered DAT target set, or only the games you own |
`rating_active` = `min_rating` or `top_n` set. **With neither set nothing changes and no rating data is needed** (`select` never looks at ratings).

## C. The rule (`library.select(items, profile, platform, ratings=None)` -> `_select_base` + `apply_ratings` + `_vanished`)
* Applied AFTER every other rule (exclusions, language, keep-flags, one-best-variant / one-per-game, completeness, borrowing) at GAME level. A
  game = `library.game_key` (as totals); all variants / disks / discs of one game share ONE rating (looked up by `library.rom_title`: the tags
  title - year, publisher, regions, languages, versions, disk / disc numbers stripped); a disk borrowed from another edition belongs to the game of
  the set it completes (a playlist is never split). Only DATs in `ratings.rated_dats(platform)` take part (Amiga: *Games - [ADF]* only; Workbench /
  Kickstart-Disks / Firmware are not games and always stay).
* rated = the title matches (section E) AND votes >= `min_votes`. `min_rating`: rated and `rating10 < min_rating` -> `rating_low` (inclusive threshold).
  `top_n`: rank key `(-rating, -votes, title.casefold(), stable game key)`; the cutoff is the key of the N-th best complete rated game at or above
  `min_rating`; a rated game after it -> `rating_not_top` (compared by `(-rating, -votes, title)` only, so every game with the title of the N-th game
  stays or goes together: "top 300" may keep a few more). No usable rating -> `rating_unrated` unless `keep_unrated`.
* `rank_scope == "dat"`: the cutoff comes from the whole DAT target (`library.target_cutoff(target_items, profile, platform, lookup)`: the games every
  other rule keeps with every rom present) and is passed in `RatingContext.cutoff` (`INF_KEY` = fewer than N rated games, no cap), so adding files never
  evicts others and totals read "have K of N". `"owned"`: the cutoff is computed from the games kept in THIS call.
* Reason codes (`library.RATING_CODES`, part of `ALL_CODES`): `rating_low`, `rating_not_top`, `rating_unrated`; excluded files go to `_excluded/` like every
  exclusion, `Decision.codes` = every code that applies, `Decision.detail` = `rated 6.2 (12 votes)` / `no usable rating`; `ChosenSet`s and kept
  `IncompleteSet`s of excluded games vanish. `Selection.rating` = `{min_rating, top_n, min_votes, keep_unrated, rank_scope, games, rated, unrated, kept,
  excluded, excluded_by: {code: games}}` (empty without a filter). `reason_counts` gains `excluded_rating_*` (ALL_CODES). `Vanished.detail` + `vanish_report`
  items gain `hint` ("lower the minimum rating" / "raise Top N" / "tick Keep unrated games (or lower Minimum votes)") and `detail`; rating codes sort FIRST in
  the vanish priority (they only ever hit variants that passed everything else).
* `RatingsUnavailable` is raised (never a silent pass) when a filter is set and `ratings` is None (or `top_n` + scope "dat" without a cutoff).
* **Idempotence** holds for both scopes (property tests: 150 random profiles x name sets, shuffled inputs, Amiga multi-disk): re-selecting the kept output
  changes nothing. In scope "owned" the kept output of run 1 is a prefix of the ranking, so the N-th key is unchanged.
* `plan_library(..., ratings=)`, `_plan_core`, `discsys.plan_library` / `plan_units(..., ratings=)` pass the context through.

## D. The index (`ratings/ratings.sqlite` in `paths.ratings_dir()`, its own sibling of dats/ nointro/ whdload/ redump/; `manifest.json` beside it)
`entry(plat, key, rating REAL stars, votes, dbid, name, year, alt)`, PRIMARY KEY `(plat, key)`; `meta(k, v)`. `key` = `ratings.norm_title`. One row per
(platform, key): own name beats an alternate name, then MOST votes, then lowest DatabaseID (documented choice instead of a vote-weighted mean). Unrated games
(rating absent / 0, no votes) are not stored. Manifest: `{schema 1, built_at, url, etag, last_modified, downloaded_at, games, entries, platforms{lb: rows},
parse_seconds}`. Measured 2026-10-04: **22k rated games, 34,449 rows, 3.1 MB, parse 25 s (29 s with download), peak RSS 65 MB** (`iterparse` + `clear()` straight out of
`zipfile.open`; the 108 MB zip is kept only as `Metadata.zip.part` while it is parsed and then deleted; the index is written to `ratings.sqlite.part`,
validated - rows present, zip valid, `Metadata.xml` present - and `os.replace`d, so a failure leaves the old index). Scale shown: `rating10 = round(stars * 2, 1)`.
`ratings.lookup(platform, title) -> (rating10, votes) | None` (cached; `Store.detail` adds `dbid, name, year, kind, score`).

## E. Matching (`ratings.norm_title`, `Store._match`): conservative, deterministic, same platform only
1. `exact`: NFKD without accents, `&` = and, a trailing `, The/A/An` of every `-` / `:` / `/` segment or a leading article dropped, apostrophes removed, other
   punctuation = space (so `Legend of Zelda, The - A Link to the Past` = `The Legend of Zelda: A Link to the Past`), case-insensitive.
2. `alt`: the same key from a `GameAlternateName` (only when no game is itself called that).
3. `roman`: II..IX / XI / XII read as digits (`roman_key`), accepted only when exactly one LaunchBox game results.
4. `fuzzy`: difflib ratio >= 0.92 among titles sharing a >= 4 letter word and a similar length; refused when the numbers differ (`number_signature`: digits AND
   roman numerals, so `Street Fighter V` != `II`, `Mega Man VI` != `7`), when one title only ADDS whole words (`DX`, a subtitle, a hack tag), when the titles
   differ only by a singular / plural, or when the runner-up (a different game) is within 0.04 (not unique). Missing a rating is preferred to a wrong one.

## F. Download / update (`ratings.check_update`, `download_and_build`; `autoupdate.UpdateManager`)
* Nothing is fetched unless `ratings_wanted()` (a rating filter in any platform's saved profile - the server sets `updates.ratings_wanted`) or the user presses
  **Download ratings** (`UpdateManager.request_ratings`, no gate, queued behind a running update). Startup: `_do` ends with `_do_ratings`; an index younger than
  7 days (`REFRESH_DAYS`) is not even asked about; otherwise one HEAD (`Last-Modified` / `ETag`); the 108 MB download only when newer. Missing index = download.
* Status block `status()["ratings"]`: `{managed, wanted, status (absent|up_to_date|update_available|updating|error), installed (source date), built_at, games,
  entries, size, parse_seconds, latest, checked_at, offline, error, credit, credit_url, age_days}`; progress uses `progress.source == "ratings"`
  (messages "Downloading the LaunchBox ratings" / "Building the ratings index", bytes of `Metadata.xml`). Ratings errors never fail the DAT update
  (`status["error"]` stays None); offline with an installed index is quiet (`offline: true`), without one `error` says so. `updates.json` gains
  `ratings.checked_at`. `UpdateManager(ratings=, ratings_wanted=)`; `_has_cache(("ratings", ()))`.
* If a filter needs data that is not installed: `GET /api/library/totals` answers `ratings_pending: true` (no number), `POST /api/library/plan|apply|vanished`
  answer **409** `{"error", "code": "ratings_pending"}` (the server also requests the download; message says so, or that automatic downloads are off).
  The UI shows a banner with progress, polls, and re-runs a waiting preview when the download ends (`Previews.retryPending`). Errors carry `code` now.

## G. Totals (Amendment 17 additions)
`compute_target(platform, dats, profile, lookup=None)`: with a filter the target is `_select_base` + `target_cutoff` + `apply_ratings` (so **M counts games after ALL
rules incl. rating**, scope "dat"); `Target.cutoff`, `.rating`, `.coverage` (`{games, rated, ge: [n games rated >= 0.5 .. 10.0]}`, computed whenever ratings are
installed, filter or not). Scope "owned": M = every game that passes the other rules and `min_rating` (top_n dropped, "rated" required via `min_rating = 0.1`), because
the cap follows the user's files; `answer.rank_scope == "owned"` makes the UI say so. `compute_user_items(..., ratings=RatingContext(lookup, target.cutoff))`:
rating-excluded owned games are `owned_but_excluded` (`have + excluded + incomplete + outside == owned_games` still holds, `have <= target`). Answer keys
added: `rating`, `rating_coverage`, `ratings_pending`, `rank_scope`. `TotalsManager(load_dats, persist, rating_lookup)`; the DAT signature of a request includes the
index identity (`built_at`), so a rebuilt index recalculates; persisted records gain `rating_coverage` only when known.

## H. Endpoints / UI
* `POST /api/library/profile` accepts `min_rating` (number 0-10 / null / ""; 0 = off), `top_n` (whole number >= 1 / null / ""), `min_votes` (whole number >= 1),
  `keep_unrated` (JSON boolean), `rank_scope` (`dat` | `owned`); a wrong type or range = **400**, nothing is saved. `GET /api/library/profile` adds
  `available.ratings`, `rating_codes`, `reason_labels` and catalog entries with `group: "ratings"` (kinds `number` {min, max, step, unit, integer, filter, summary,
  hint}, `option`, `choice` {choices}); the JS renders them generically (no rule list in the script; test `UiContractTests`).
* `GET /api/ratings` (status block + `supported`, `refresh_days`), `POST /api/ratings/download` (`{started, ratings, updates}`; `started: false` when updates are off or one
  runs). `POST /api/library/plan` adds `rating` (the `Selection.rating` summary); `plan_id` carries the index identity while a filter is set.
* `GET /api/scan/results?kind=games` rows gain `title`, `rating` (0-10), `votes`, `rating_match` (None = unrated / not installed) and accept `rated=1|0` and `sort=rating`
  (best first, unrated last); computed once per scan + index.
* Library tab, rules panel group **Ratings**: Minimum rating, Top N, Minimum votes (number fields, empty = off), Keep unrated games, Rank against (select); credit
  link; status line "Ratings found for X of Y target games (Z%) · LaunchBox data from <date>" + **Download / Update ratings**; live hint "≈ N of M target games are
  rated ≥ x" from `rating_coverage.ge`; the note "Games with no rating are excluded while a rating filter is set (tick Keep unrated games to keep them)". Rules
  summary: `rated ≥ 7 · min 5 votes`, `top 300`, `keep unrated`, `ranked among your games`. Changing a field goes through the same `profileChanged` path (preview
  stale, totals recalculate in the background). Preview: card "Left out by rating", chips per reason, vanish hints incl. a "Keep unrated games" quick fix.
  Browse > Games: Rating column ("8.4 · 123 votes" / dash, sortable header) and Rated / Unrated chips (only for systems with ratings).

## I. Measured on the real data (LaunchBox 2026-10-04, DATs of 2026-10-02..04; default profiles; min_votes 5)
Coverage of the DAT titles (title strings of the rated DATs) and of the default-rules target set M (games; "rated" needs votes >= 5):
| system | titles | rated | exact / alt / roman / fuzzy (all titles) | M | M rated | exact / alt / roman / fuzzy / none (M) |
|---|---|---|---|---|---|---|
| Commodore Amiga (Games ADF) | 4,042 | 76% | 65 / 10 / 0.2 / 1.3 % | 3,591 (+65 non-game disks) | 1,726 (48%) | 71 / 10 / 0.3 / 0.7 / 19 % |
| Amiga - WHDLoad | 2,960 | 82% | 67 / 10 / 3.2 / 2.1 % | 2,763 | 1,590 (58%) | 73 / 9 / 3.4 / 1.5 / 13 % |
| Nintendo 64 | 602 | 86% | 66 / 20 / 0 / 0 % | 402 | 336 (84%) | 77 / 7 / 0 / 0 / 15 % |
| NES | 4,099 | 66% | 49 / 17 / 0.1 / 0.4 % | 2,650 | 1,667 (63%) | 59 / 12 / 0 / 0.3 / 28 % |
| GBA | 2,331 | 86% | 60 / 26 / 0 / 0.9 % | 1,438 | 1,242 (86%) | 73 / 16 / 0.1 / 0.5 / 11 % |
| Dreamcast | 959 | 82% | 60 / 21 / 0 / 1.3 % | 381 | 343 (90%) | 77 / 14 / 0 / 0 / 9 % |
| PlayStation | 6,440 | 79% | 54 / 22 / 0 / 2.4 % | 2,168 | 1,938 (89%) | 78 / 15 / 0 / 0.2 / 7 % |
| PlayStation 2 | 6,677 | 68% | 48 / 18 / 0 / 1.6 % | 3,177 | 2,623 (83%) | 75 / 13 / 0.1 / 0.4 / 12 % |
| SNES | 2,423 | 95% | 70 / 24 / 0.1 / 0.2 % | 1,110 | 1,015 (91%) | 77 / 16 / 0.2 / 0 / 7 % |
Targets under filters (min_votes 5): `min 7, unrated off` / `min 7 + keep unrated` / `top 300 (dat)` / `top 300 (owned scope: every rated game)`: Amiga 939 / 2,804 / 365 (300
games + the 65 unrated-by-design system disks) / 1,791; WHDLoad 855 / 2,028 / 300 / 1,590; N64 163 / 229 / 300 / 336; NES 788 / 1,771 / 300 / 1,667; GBA 457 / 653 / 300 /
1,242; Dreamcast 233 / 271 / 300 / 343; PlayStation 1,024 / 1,248 / 300 / 1,926; PlayStation 2 1,950 / 2,501 / 300 / 2,616; SNES 413 / 508 / 300 / 1,015. Precision (hand-read
samples of 40 random matches per system and of the fuzzy-only matches): no clearly wrong exact match; alternate names are LaunchBox's own and right in nearly every
sample (a few arguable: `Daytona USA 2001` -> `Daytona USA`, `Count Duckula` -> `Count Duckula in No Sax Please: We're Egyptian`); fuzzy matches are almost all spelling /
spacing variants (`Wonderboy` / `Wonder Boy`, `Hugo - CannonCruise`); doubtful ones kept as matches: `Rainbow Warriors` -> `Rainbow Warrior`, `Vroom Multiplayer` -> `F1`,
`Emerald Mines` -> `Emerald Mine`. The first fuzzy version produced real errors (`Street Fighter V` -> `II`, `Mega Man VI` -> `7`, `Sigma Star Saga DX` -> `Sigma Star Saga`) which
the number / added-word / plural rules now refuse.

## J. Verified
Unit tests (`tests/test_ratings.py`, 75 tests, ~2 s): parser / index on a synthetic `Metadata.xml` (alt names, zero ratings, duplicate names, few votes, other platforms),
matching, 7-day gate / offline / corrupt download / cancel, every rule (min rating, min votes, top N both scopes, ties, keep_unrated, multi-disk, variant / language
interaction, vanish hints, defaults unchanged, idempotence), totals, updater orchestration, server validation, endpoints, Browse column, UI contract. Headless Brave at
1280x800 (throwaway profile, a No-Intro N64 DAT copy with 70 synthetic ROMs): Ratings group, live hint, stale banner + totals recalculation after setting a rating, Preview with
the rating cards / chips / vanish list with hints, top N + keep unrated, rank scope "owned" note, Browse rating column + chips + sort, the "ratings pending" banner (no data),
and the REAL startup flow (filter saved, no index: the app downloaded the zip, showed "Building the ratings index (n%)", then calculated the totals by itself).
Not verified in a browser: touch hardware, a gamepad, screen readers; the "Download ratings" button while updates are enabled was exercised through the real startup
flow and unit tests only.


# Amendment 19 - sortable lists, per-game overrides, remembered views, planner speed, browser tests

Binding. Everything here is additive; no existing contract changed.

## A. Sorting (Browse and Library)
`GET /api/scan/results` (`sort=`) and `POST /api/library/plan` / `POST /api/library/vanished` (`sort` in the body) take one of
`rating_asc | rating_desc | name_asc | name_desc | year_asc | year_desc | size_asc | size_desc` (`rating` alone = `rating_desc`; anything else keeps the
natural order). Rows without the sorted value (unrated, no year, no size) are ALWAYS last, whichever way it runs; ties fall back to the votes (ratings), then the name.
`server._sort_rows` is the one implementation. Browse > Games rows carry `year` (TOSEC date paren only) and `size` (the DAT rom's size); the page carries `has_year`.
The matched / unmatched / missing lists sort by name and size. Library rows carry `rating`, `votes`, `year`, `size`, `tags`, `game_ref` ({dat, game}) and
`cs_kind` / `cs_id` (the scan result row whose checksums `/api/scan/checksums` shows); `checksums: true` in the plan body inlines them for the page.

## B. Per-game overrides
`LibraryProfile.overrides` = `((dat, game, "keep" | "exclude"), ...)`, `game` being the DAT's own name for the game (No-Intro / Redump: the set name; TOSEC: the rom name
with its extension - the `name` of a Browse > Games row). `library.apply_overrides` runs after every rule and the rating filter and before the vanish report: `keep` keeps the
file whatever excluded / superseded / left it out (code `override_keep`), `exclude` sets it aside (action `excluded`, code `override_exclude`, so it goes to `_excluded/`);
a playlist whose disk is excluded this way is dropped. Stored in the profile (`overrides` in `to_dict`, hence in the totals signature); `POST /api/library/override`
`{platform, action: keep | exclude | clear, games: [{dat, game}]}` (1-5000 games) edits it. UI: tick Library rows, then Always keep / Always exclude / Back to the rules;
the preview is marked out of date (Recalculate applies it).

## C. Row details
Each Library row has a Details toggle (and the global "Show checksums" switch opens all): a plain-language why (rule that fired, version that won, rating, region,
language) plus the DAT / file checksums. The panel is built when opened.

## D. View settings
Per system, in `localStorage` (`romorg.prefs.v1`, always optional): the sorts, the Have / Rated / Library reason / status / tag filters; globally the compact view; per table
the hidden columns (the Columns menu). Table headers stay visible while the rows scroll (the table scrolls inside a bounded box).

## E. Planner speed (measured with `tools/bench_server.py`, Amiga 31,754 matched files, warm hash cache, Steam Deck; before -> after)
scan 12.0 -> 8.5 s (the hashing is cached: the rest is the summary); first Library preview 15.0 -> 10.2 s; preview after a rule change 13.5 -> 8.5 s; Browse > Games first page
7.8 -> 6.4 s (about two thirds is the one-off parsing of 29k TOSEC names, shared with the plan); sorting, paging and searching 0.01-0.15 s (precomputed search text, no cache
needed). Changes: path arithmetic by string instead of pathlib (`organiser._rel_text`, `scanner._rel_parts`, `Entry.rel`); the planner-independent units of a finished
scan are built once and handed out as copies (`result.cache_units`, set by the server only: a plan edits its ops); `safe_filename` / `canonical_dir` memoised; the ratings
store looks at its index file twice a second, not per lookup; a new scan first drops the previous scan's derived caches (pages, plans, checksum index) so the old and the new
scan never share memory. Not done: re-planning only the games a rule touches (the selection depends on the whole profile; 3 s of the 8.5 s is `library.select`).

## F. Browser tests
`tests/test_browser.py` drives a real headless Chromium / Chrome / Brave (or the Brave flatpak on the Deck, or `ROMORG_BROWSER_PORT`) through a tiny DevTools-protocol client
(`tests/cdp.py`, stdlib only) against a synthetic Amiga + GBA library (`tests/uifixture.py`); it fails on any JavaScript error and is skipped without a browser or with
`ROMORG_NO_BROWSER=1`.

## G. Second pass (found by driving the real UI on the real folders)
Speed: `tosec.list_dats` (4,743 files, behind every status / platforms / profile / totals request) uses `scandir` and reuses its answer while the file names are
unchanged: `/api/status` 130 -> 45 ms, `/api/platforms` 140 -> 40 ms. The page asks for status and platforms once at start (was twice) and shares one undo-log request.
`GET /api/organise/undo-logs` counted a log's changes by fully reading it (path checks included): 13.5 s for the Amiga log of 33k moves, on every Library / Tools open;
`organiser.count_undo_log` counts records only (0.24 s) and the server remembers the count per (path, size, mtime). `read_undo_log` (the real Undo) keeps its symlink-escape
check but resolves each folder once per read: 12.5 -> 6.3 s. `m3u.one_look()` (thread-local) makes one library plan walk the folder for playlists and read each playlist once.
UI: Browse > Games lost its Rating column and chips when the list was drawn before the profile arrived (columns now carry `when`, and the list is redrawn when the profile
lands); the rating bar's width was an inline style the CSP refused (set through the style object); sort-button headers sat lower than plain ones; names broke mid-word
(`overflow-wrap: anywhere`); the Library "Why" column was squeezed into tall rows; an in-place rename showed the badge "move"; the Columns menu closes on an outside click /
Escape; Compact shrinks the row buttons; the Incomplete / Vanish boxes show a disclosure marker. `POST /api/library/plan` also returns `categories` (rows per category,
moving or already in place): the reason chips use it, and the cards say "Excluded now (N already set aside)" - on an already built library the chips used to offer no
Excluded / Superseded filter at all. Browse > Matched shows "set aside" (row field `aside`), not "move", for files in the app's own folders.


# Amendment 20 - Zstandard CHDs

`chd.py` decodes chdman's `cdzs` (CD: sectors and subcode are Zstandard frames, same ECC bitmap header as `cdzl`) and `zstd` (plain hunks, DVD CHDs)
codecs through `romorg/zstdnative.py`, which loads a Zstandard decoder and compiles nothing: `compression.zstd` (Python 3.14+), else libzstd through ctypes
(`$ROMORG_LIBZSTD`, the AppImage's `tools/lib`, a `libzstd.dll` / `zstd.dll` next to the Windows app or in its `native` folder, the system library - present on
every SteamOS / desktop Linux, and already required by chdman). With no library, or `ROMORG_NO_ZSTD=1`, such a hunk raises `ChdUnsupported` with `needs_chdman`
exactly as before, so the chdman fallback is unchanged. Damaged Zstandard data is `ChdError` (corrupt), not "unsupported". Still not decoded: `huff`, `flac`
data hunks and `avhu` (chdman's default `createdvd` codec list contains `huff` and `flac`; a hunk that really uses one sends that file to chdman).
Checked against chdman 0.289 on real discs re-compressed with `-c cdzs,cdzl,cdfl`, `-c cdzs` and `createdvd -c zstd`: every track hash identical to the
original, the raw SHA-1 (with subcode) matches; speed with 8 workers on the Steam Deck 220-335 MB/s (the same PlayStation disc as `cdlz`: about 100 MB/s).
`python -m romorg.selfcheck` reports the library. The Windows builds ship Python 3.13 and no libzstd, so there Zstandard CHDs still go to chdman until the
build moves to Python 3.14 or bundles a `libzstd.dll`.

# Amendment 21 - The reader reads everything chdman reads

Goal: no CHD needs chdman to be *read* (creating CHDs still does). What was missing after Amendment 20, and how each is done:

| What | How | Checked against |
|---|---|---|
| `huff` hunks | `romorg/chdhuff.py`. The tree is read in Python; the codes are handed to **zlib**: a deflate dynamic block is canonical Huffman codes for byte values, and MAME's codes are deflate's mirrored (MAME numbers the longest codes from zero), i.e. the same tree on the complemented, per-byte bit-reversed stream with the symbols of each length reversed. Deflate needs an end-of-block code that MAME's full tree has no room for, so the all-zero longest code shares its place with it and decoding restarts behind each occurrence of that (rare) symbol. Trees deflate cannot hold (15/16-bit codes, incomplete) and flat trees (too many restarts) use the plain loop. | chdman 0.289 `createraw -c huff` and default codecs |
| `flac` data hunks | `L`/`B` byte + FLAC frames through `flacnative` | chdman default `createhd` / `createdvd` / `createraw` |
| `avhu` (laserdisc) | `chdhuff.avhuff_decode`: metadata, FLAC / Huffman / raw audio, delta-RLE Huffman YUY2 picture | chdman `createld` (mono and stereo, FLAC audio); Huffman and raw audio only on synthetic hunks |
| Parent files | `Chd(path, parent=...)`, else the CHD with the wanted SHA-1 in the child's folder (`chd.find_parent`, headers only), opened on the first hunk that needs it. v5 compressed (`PARENT`, `PARENT_SELF`, `PARENT_0/1`: unit offsets), v5 uncompressed (an unwritten hunk is the parent's, or zeros without one), v3/v4 (parent hunk number). A child of a parent without checksum (uncompressed) can only be given by hand, as with chdman. | chdman `-op` children of compressed and uncompressed parents |
| Versions 1 to 4 | 8-byte (v1/v2) and 16-byte (v3/v4) map entries, entry types compressed / uncompressed / mini / self / parent, MD5-only v1/v2, unit size from the metadata, `CHCD` binary and `CHTR` text CD track lists | **synthetic files only** (`chdtestlib.build_old_chd`, written from MAME's format description): no current chdman writes these |
| Zstandard without a library | `romorg/zstddec.py`, a pure-Python RFC 8878 decoder (about 1 MB/s) behind `zstdnative` | libzstd: 976 real hunks and 600 fuzzed frames (levels -5 to 22), identical |
| `chdman verify` | `Chd.verify()`: data SHA-1 and the overall SHA-1 (data + sorted hashes of the flagged metadata); `None` for a CHD without checksums | every fixture |

`ChdUnsupported.needs_chdman` is now only set for a compression name the reader does not know (a later MAME); the chdman fallback in `discsys` stays for that case and for the forced `chdman` engine.

Real files written by chdman 0.289 from generated content are in `tests/fixtures/chd` (`make_fixtures.py` there shows how); `tests/test_chd_formats.py` uses them.

Speed (Steam Deck, 8 threads; chdman 0.289 timed on the same files):

| File | chdman `extract*` | chdman `verify` | reader, 1 process 1 thread | reader, 1 process 4 threads | scheduler, 8 workers, all tracks hashed |
|---|---|---|---|---|---|
| CD, FLAC audio only (420 MB) | 3.9-4.3 s | 5.8 s | 4.5 s | 2.8 s | 1.5 s |
| DVD, chdman's default codecs, 410 MB of PS2 data (16 % `huff` hunks) | 3.8-4.0 s | 5.8 s | 7.2 s | (no threads: 4 KiB hunks) | 2.6 s |

FLAC: one core is bound by the same libFLAC chdman uses, so the gain comes from decoding in parallel. `Chd.threads` (default up to 4, `ROMORG_CHD_THREADS`; 1 inside the scheduler's workers) decodes several hunks at once for hunks of 16 KiB and more. The libFLAC binding calls back into Python for every read and write, which keeps threads queueing for the GIL, so when only the audio is wanted the hunk goes to libsndfile through an anonymous memory file (`nativeflac.decode_frames_fd`, Linux): no callback at all, 94 -> 311 MB/s from 1 to 4 threads in isolation. Audio tracks are decoded straight to CD byte order (one byte swap less). A hunk that many others copy (silence, zeros) is decoded once.

Known limits: `huff` in pure Python + zlib is about 14 MB/s per process (chdman: far more), so a default-codec DVD CHD read by ONE process is slower than chdman; the scheduler's workers make up for it. `avhu` video is a Python loop (about 1 MB/s). Nothing here was run on Windows.

# Amendment 22 - The app writes CHDs itself

`docs/CHD_WRITER_PLAN.md` was carried out for its tier 1 (what the app converts): `romorg/cdimage.py` (inputs), `romorg/chdwrite.py` (container, codec choice, parallelism), `romorg/flacenc.py` (libFLAC encoder), `cdecc.valid` (which sectors carry standard ECC), a `compress` request in `romorg/chdworker.py`.

**Same image as chdman.** For the same set the CHD has chdman's data SHA-1, metadata text and header SHA-1 (which covers both), `chdman verify` accepts it, and the reader gets the source tracks back. Checked for 16 layouts (`tests/fixtures/chdwrite/layouts.py`: Redump multi-file cue with `INDEX 00`, single-file cue, `PREGAP` / `POSTGAP`, cooked `MODE1/2048`, `MODE2/2336`, two data tracks, audio only, three GDIs, ISO as CD and as DVD) against chdman 0.289; its answers are stored in `chdman_reference.json`, so the suite needs no chdman. Layout rules as chdman has them: frames of 2448 bytes with zero subcode, audio big-endian, tracks padded to 4 frames, `INDEX 00` = pregap inside the track (`PGTYPE:V<type>`), `PREGAP` / `POSTGAP` commands metadata only, a GDI track runs to the next one (`PAD` zero frames). A cue or GDI the parser is not sure about is refused (`ImageError`), never guessed; an installed chdman then gets the set.

**Compressed bytes are our own**, except FLAC: libFLAC with MAME's settings (level 8, block size = samples halved to at most 2352 for `cdfl`) gives frames byte-identical to chdman's. MAME's decoder rejects frames with another block size, so there is no fallback encoder: without libFLAC audio goes to `cdlz`. LZMA is Python's `lzma` (`FORMAT_RAW`, the dictionary MAME's decoder expects, `nice_len` 32).

**Codec choice.** chdman tries every codec on every hunk. Here: a copy of an earlier hunk is found by SHA-1 before compressing; audio hunks get FLAC only; a data hunk is probed (deflate level 1 on two 2 KiB samples) - if that does not compress, deflate and no LZMA, else LZMA and no deflate. Measured on PlayStation / PlayStation 2 / Dreamcast hunks, not trying deflate everywhere costs 0.14-0.45 % in size (deflate wins many hunks, each by very little).

**Parallelism.** Worker processes (the `chdworker` ones), fed by threads; batches overlap so the parent reads and hashes while the workers compress. Plain threads were tried first and are the fallback (small images, no worker): LZMA releases the GIL, but the ECC check is Python arithmetic (about 0.4 ms per hunk) and eight threads queue for the interpreter behind it.

Measured (Steam Deck, 8 threads, chdman 0.289 on the same files; every result has chdman's SHA-1 and passes `chdman verify`):

| Input | chdman | built-in, standard | built-in, Zstandard |
|---|---|---|---|
| PlayStation CD, 429 MB, one MODE2 track | 15.8 s, 285.4 MB | 15.8 s, 286.4 MB (+0.35 %) | 4.2 s, 286.0 MB |
| Dreamcast GD-ROM, 1.2 GB, with audio | 50.7 s, 662.3 MB | 44.4 s, 663.6 MB (+0.20 %) | 13.3 s, 690.4 MB (+4.2 %) |
| PlayStation 2 ISO, 1.5 GB, as DVD (`createdvd`) | 91.2 s, 1268.8 MB | 39.7 s, 1273.7 MB (+0.38 %) | 24.2 s, 1287.8 MB (+1.5 %) |
| the same ISO as CD (`createcd`) | 66.4 s, 1323.3 MB | 33.4 s, 1323.9 MB (+0.04 %) | - |

So: never slower, about twice as fast where data does not compress or repeats, and level on a disc where every hunk is unique and compressible (both tools are then bound by LZMA at the same speed per core; chdman's encoder is a little quicker than liblzma, the saved deflate trials make up for it).

**In the app.** `discsys.apply_conversions(..., writer="auto" | "chdman", preset="default" | "zstd")`; settings `chd_writer`, `chd_preset` (`POST /api/chdman`). `auto` = built-in, chdman only for a set the built-in parser refuses. The flow around it is unchanged: `.part` file next to the target, kind check, every track decoded again and compared with Redump, originals kept, undo. The AppImage no longer ships chdman (libFLAC / libogg stay); an installed chdman is detected as before.

Not done / not known: the writer was never run on Windows; no emulator was started on a written file (the evidence is SHA-1 equality with chdman, `chdman verify`, and the reader); tiers 2 and 3 of the plan (hard disks, parents, `huff` / `flac` data hunks, laserdiscs) are out of scope by the user's decision.

# Amendment 23 - The Windows pass

The CHD engine of Amendments 20-22 was written and measured on Linux. This pass ran it on Windows 11 (16 threads, Python 3.14) and brought the Windows packages to the same features, so that **Windows needs no chdman either**. Decisions:

**Python 3.14 in both Windows builds.** The zip bundles the embeddable CPython 3.14 (`build_windows.ps1 -PyVersion`, default 3.14.8), the exe is built with `py -3.14` and a pinned PyInstaller (`build_windows_exe.ps1 -PyInstallerVersion`). `compression.zstd` then reads Zstandard CHDs at library speed and makes the Zstandard preset available, with no `libzstd.dll` to ship. The note at the end of Amendment 20 (Windows sends Zstandard CHDs to chdman) no longer holds.

**libFLAC 1.5.0 is shipped.** The official Xiph.Org Win64 `libFLAC.dll` (SHA-256 pinned in `packaging/fetch_flac.ps1`) goes into `app\native` of the zip and `native\` inside the exe; `flacnative._candidates()` finds it there (and in `_MEIPASS`). Without it the writer stores audio tracks with LZMA: valid, but measured against chdman's size 1.0885x instead of 1.0063x (Alienfront Online) and 1.0044x instead of 1.0032x (Dead or Alive 2). The DLL is a MinGW-w64 build that links libogg, winpthreads and the MinGW-w64 runtime statically, so their licence texts ship with it (`docs/THIRD_PARTY.md`). libsndfile stays in the zip as a stand-in decoder; the UI and the self-check name it when it is the one decoding.

**chdman is not needed and not shipped on Windows.** Nothing chooses chdman by platform: the built-in reader and writer are the defaults everywhere, and chdman stays an option (`chd_engine` / `chd_writer`, the read fallback for an unknown codec, a layout the writer refuses). It is found on `PATH`, next to the app (the exe's folder, or the zip's top folder: `winproc.app_dirs`), in common MAME / tool folders, or at the saved path. The install hint is per platform (`chdtool.install_hint`: chdman.exe from the MAME download at mamedev.org on Windows, the Discover / Flatpak step on the Steam Deck); no text says converting needs chdman. `/api/chdman` and `/api/status` report `os`, `/api/chdman` also `flac_encoder`, and the Convert step says when audio will be stored with LZMA.

**Open handles.** Windows refuses to rename or delete a file another process holds. The reader's worker processes keep a few CHDs open (`chdworker.MAX_OPEN`), so the CHD a conversion has just verified could not be moved into place (WinError 32) while the scheduler lived on. The workers are made to close a CHD before it is renamed or deleted (convert, undo). On Linux nothing changes in behaviour.

**No `taskkill` for CHD workers (from the interpreter).** Workers start no processes of their own, so they are closed by closing their input and waiting; a worker still running is then terminated by `TerminateProcess` (`chdsched._terminate`). The frozen one-file exe is the exception: its bootloader runs the worker in a child process, so a worker that is still running there is killed with `winproc.kill_tree` (`taskkill /T`); a worker that has already exited is left alone in both cases. In the phase-1 profile (before this change and the open-handle fix above) `taskkill /T` cost about 0.19 s per worker, run one after another: 2.4-2.7 s per pool and about 5 s per converted disc (one writer pool, one verify pool); those figures describe the old behaviour, not the current one. `winproc.kill_tree` stays for chdman and 7-Zip, which can start children.

**Smaller Windows fixes.** `os.kill(pid, 0)` sends `CTRL_C_EVENT` on Windows instead of probing, so the scratch-folder sweep (`tempspace`, `chdtool`) uses `winproc.pid_alive`: `OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)` + `GetExitCodeProcess == STILL_ACTIVE`, access denied counted as alive, and a probe that fails for any other reason also counted as alive (the caller would otherwise sweep a live owner's folder); POSIX keeps `os.kill(pid, 0)`, where only `ESRCH` means dead. A browser that closes a connection mid-response (WinError 10053) ends that connection quietly (`Handler._send`) instead of logging a traceback and trying a 500 on the dead socket. The self-check has a Windows package check (`selfcheck.check_windows_package`, when frozen or when `app\native` exists): the shipped libFLAC must be the one loaded and must encode, Zstandard must come from a library, a shipped libsndfile must load; the builds run it with `--require-native`.

Measured on that machine (phase-1 profile, 12 workers, libFLAC, chdman 0.289 on the same sets; every written CHD had chdman's SHA-1, the engine was always `processes`), before the worker-close and buffered-reply fixes:

| Input | chdman | built-in, standard | built-in, Zstandard |
|---|---|---|---|
| PlayStation CD (Spider) | 8.76 s | 8.78 s | 6.85 s |
| Dreamcast GD-ROM (Dead or Alive 2) | 22.73 s | 17.01 s | 12.53 s |
| Dreamcast GD-ROM (Alienfront Online) | 21.34 s | 16.31 s | 11.86 s |
| PlayStation 2 ISO as CD | 85.41 s | 42.06 s | 32.70 s |
| PlayStation 2 ISO as DVD | 110.17 s | 58.98 s | 60.72 s |

With both fixes patched in, the same runs took 6.3-6.7 / 13.5-16.3 / 13.8 / 35.4 / 28.3-36.6 s (standard). Reading (the scheduler, verify path) was 4-8x faster than `chdman verify` once workers close promptly. The worker count is flat from 8 to 16 for writing, so `WINDOWS_MAX_AUTO = 12` stays.

# Amendment 23 - Eight more No-Intro systems

Added to `platforms.PLATFORMS` / `nointro.NOINTRO_DATS` / `ratings.LB_PLATFORMS`: Game Boy, Game Boy Color, Nintendo DS, Sega Mega Drive - Genesis, Master System, Game Gear, 32X and Atari Lynx. Each is one `_nointro(...)` entry: no alternate-hash strategy, not convertible, folder names `gb gbc nds megadrive mastersystem gamegear sega32x atarilynx` (all already in `server.OTHER_SYSTEM_DIRS`).

Why no header handling, checked on the libretro DATs of 2026.08.01: every rom is hashed as it is. The Lynx DAT carries the headered `.lnx` (size % 1024 == 64) and the headerless `.lyx` / `.bll` as separate roms of one game (235 of 364 sets have alternates), so both kinds of dump match raw; the 7800 and Famicom Disk System DATs do the same (not added yet). `.smd` Mega Drive dumps (512-byte header, interleaved) are not in the DAT and stay unmatched; `.smd` is therefore not in the extension hints.

| DAT | rom blocks | sets | alternates |
|---|---|---|---|
| Nintendo - Game Boy | 2254 | 2254 | 0 |
| Nintendo - Game Boy Color | 2566 | 2566 | 0 |
| Nintendo - Nintendo DS | 7701 | 7693 | 8 |
| Sega - Mega Drive - Genesis | 3365 | 3365 | 0 |
| Sega - Master System - Mark III | 1163 | 1163 | 0 |
| Sega - Game Gear | 915 | 915 | 0 |
| Sega - 32X | 207 | 207 | 0 |
| Atari - Lynx | 681 | 364 | 235 |

Region tags are recognised on every name of these DATs (a full stop after a closing bracket, as in "(Unl).", is ignored; the same rule gives ten TOSEC names ending "(AU)." a region and so a different identity key). LaunchBox's platform names (read from `Platforms.xml` inside `Metadata.zip`, 2026-10-06) are the same as ours except the Mega Drive, which LaunchBox calls `Sega Genesis`. Startup update now downloads twelve No-Intro DATs, about 15 MB the first time (the eight new ones about 6 MB), then only changed files by ETag.

Tests: `tests/test_platforms.py::test_every_nointro_platform_is_wired_up` (DAT listed, folder name known, rating name set, no two systems share a folder), `tests/test_integration.py::PlainSystemsIntegrationTest` (synthetic scan -> plan -> apply -> settled -> undo for each, including a Lynx game with a headered and a raw rom), `tests/test_datfile.py::RealNoIntroTest` (the counts above on the real DATs).

Not done: no real ROM set was scanned (the checks are the real DATs plus generated files); a Game Boy and Game Boy Color folder are two separate systems here, so a mixed `gb` folder needs scanning once per system.

# Amendment 24 - Nintendo GameCube (ISO and RVZ)

`Nintendo GameCube` is a flat platform (`layout flat`, `source redump`, `convertible False`, folder `gc`) whose DAT is the Redump DAT "Nintendo - GameCube" (`redump.SYSTEMS["Nintendo - GameCube"] = "gc"`; 2,019 games, one `.iso` rom each with CRC / MD5 / SHA-1, size 1,459,978,240 except two entries of 115,898,368 bytes; parsed by `parse_redump`, so the Library rules use the Redump name style). It does not go through `discsys`: the ordinary scanner hashes each file, and `Platform.containers = ("rvz",)` makes it hash an `.rvz` as the ISO it stands for.

**`romorg/rvz.py`.** Dolphin's RVZ (`docs/WiaAndRvz.md` in the Dolphin repository): a 0x48-byte head and a 0xDC-byte disc struct (both SHA-1 checked), a table of raw data ranges and a table of groups (`rvz_group_t`: offset / 4, size with the "compressed" flag in the top bit, packed size), groups of `chunk_size` (128 KiB in the files seen) compressed one by one with Zstandard (also bzip2, LZMA, LZMA2 or none), the first 0x80 bytes of the disc in the disc struct, an empty group = zeros, and *RVZ packing* for padding: a packed group is a list of literal runs and padding seeds, each seed starting a Lagged Fibonacci generator (j = 32, k = 521, restarted for every 32 KiB block, started at `offset % 0x8000`). Wii (`disc_type 2`) and WIA (other magic) raise `RvzUnsupported`; damage raises `RvzError`.

**The generator is the cost.** The first version (a Python loop per word) took 106 s for a 1.4 GB disc. `_forward` now advances the 521-word state in 16 big-integer XORs (the recurrence `b[i] ^= b[i - 32]` is done 32 words at a time) and `_words` extracts the output bytes with byte slices and two masked shifts: about 58 MB/s per core against 3.3 MB/s for the loop written from the spec (`tests/rvztestlib.reference_junk`, which the tests compare it with on random seeds and offsets). Sequential decode of the 1.4 GB Super Mario Sunshine image: 10 s; with worker processes (`op: "rvz"` of `chdworker`, started by `chdsched.spawn_worker`, 4 MiB of decoded image per request, hashing in order in the caller, a worker that fails hands its slice to the caller): 3.4 s with 4 workers (8 workers: 3.8 s; the caller's crc32 + SHA-1 and the pipes are then the limit).

**Checked against reality.** The three real GameCube RVZs on the Steam Deck (Super Mario Sunshine (USA, Canada), Super Monkey Ball (USA), Super Monkey Ball 2 (USA)) rebuild to ISOs whose CRC32 and SHA-1 equal the Redump entries, through the app's own DAT download and `scanner.scan`: 3 of 3 matched, 14 s cold for the three, 0.03 s with the hash cache. Synthetic RVZs from `tests/rvztestlib.build_rvz` cover zero groups, packed groups with padding at several offsets, uncompressed and Zstandard groups and tables, several chunk sizes, the worker path, a dying worker, cancel and damage.

**Measured on Windows** (Windows 11, 16 threads, Python 3.14, the same three RVZs, 2026-10-06): the 1.4 GB Super Mario Sunshine image hashes to the Redump SHA-1 in 2.35 s with the default worker count, 2.5-2.7 s with 4, 8 or 12 workers, and 7.9 s in one process; a cold scan of the three discs through the app's own Redump download takes 8.8 s (3 of 3 matched, `correctly_named` 3), a cached rescan 0.03 s. Cancelling stops a scan at once and `hash_image` 0.08 s after the cancel fires, with no worker left behind. The built exe's `--chd-worker` answers an `op: "rvz"` request with the same bytes as the in-process reader (first reply, including the exe starting, 0.41 s), and `release` and EOF work. The self-check line is OK in the dev tree, the zip and the exe (`compression.zstd`), and with `ROMORG_NO_ZSTD=1` (the built-in Python decoder).

**Scanner and naming.** `scanner.scan(..., containers=("rvz",))`: a `.rvz` with the RVZ magic gets `Entry.size = the disc size`, its decoded `crc` / `sha1` (cached under the file's own size and mtime), and `Match.matched_via = "container"` (`container = "rvz"`); a Wii or WIA file goes to `ScanResult.unsupported`, a damaged one to `errors`, a `.rvz` without the magic is hashed as a plain file. `organiser.target_filename` gives a container match the stem of the Redump name and its own extension (like the honest `.smc` / `.v64`), so `Game (USA).rvz` stays an `.rvz`; the UI chip is "rvz".

**Self-check and packaging.** `selfcheck.check_rvz` rebuilds an embedded 1.4 KB RVZ (Zstandard, a seeded padding group) and compares CRC32 and SHA-1; it runs with libzstd and with the pure-Python decoder. `romorg.rvz` is in the AppImage import check.

**Not done:** Wii (partition data is stored decrypted without hashes: rebuilding it means recomputing the H0-H3 hash tree and AES-encrypting with the partition key, which needs OpenSSL's libcrypto through ctypes), WIA, GCZ, CISO, NKit; matching a GameCube game by the ID in the RVZ header (Redump's DAT has no serials).


# Amendment 25 - Build the library in another folder (v0.2)

`romorg/libexport.py` is a second *apply stage* for the unchanged library plan. `plan_export(plan, root, dest, mode)`
derives the kept files from the plan's ops (see `kept_files`) and the destination path from the in-place target;
`apply_export` transfers them (hard link / copy / symlink, `.part` + `os.replace`) and records every created file
and folder in `<dest>/.romorg-library/library.sqlite`; `undo_run` removes only what is still unchanged. The server
adds `export_to` / `export_mode` / `export_sidecars` to `/api/library/plan|apply` and the `/api/library/export/*`
endpoints; `library_export` in `config.json` remembers the choice. The source is read only, so no re-scan follows.
The plan for ROM roots with global settings is `docs/V0_2_PLAN.md`.


# Amendment 26 - Collection: a ROM root as one library (v0.2)

`romorg/collection.py` finds the system folders of a ROM root and merges the collection's global rule fields with a
system's defaults (`effective_profile`). The server job `collection` runs, per enabled system, a scan that does not
replace the current one (`_run_scan(keep=False)`), `_make_library_plan` (the plan builder split out of `_library_plan`) and
the Amendment 25 export into `<dest>/<folder>/`. Endpoints: `/api/collection` (+ `/detect`, `/save`, `/plan`, `/apply`,
`/undo`); settings in `config.json["collection"]`. The UI is the `#/collection` view. Details: `docs/V0_2_PLAN.md`.


# Amendment 27 - Sync (v0.2)

`libexport.plan_export(sync=True)` adds `Removal`s and `replace` transfers from the manifest; `apply_export` performs
them after the transfers, re-checking each file, and records removals in the manifest's `removed` table so `undo_run` can
put them back. Guards: an empty plan never removes anything; a mass removal needs `allow_mass`. `export_sync` /
`allow_mass_removal` on `/api/library/plan|apply`, `sync` in the collection settings. See `docs/V0_2_PLAN.md`.


# Amendment 28 - RetroArch saves and config (v0.2)

`romorg/retroarch.py`: `detect_installs`, `read_cfg` / `settings_of` / `write_cfg` (key-preserving edit with a timestamped backup),
`plan_relocation` / `apply_relocation` / `undo_relocation` (files move by hard link + unlink or rename, never over an existing
file; one JSON journal per run in `<data dir>/retroarch-undo/`; optional zip backup), `is_running`, `override_warnings`.
Server: `/api/retroarch` (+ `/select`, `/plan`, `/apply` job, `/undo`), settings in `config.json["retroarch"]`.

Stages 2 and 3: `plan_follow` / `apply_follow` rename (in place) or copy (builds elsewhere, collections) the files whose content name
matches a renamed game, keeping each core folder; the journal (`kind: follow`, `library_log`) is undone with the library build.
`core_infos` / `cores_for_platform` / `check_bios` / `apply_bios` read the cores' `.info` firmware lists (`firmwareN_path`, `_opt`,
md5 from `notes`) and fill the system folder from the system's ROM folder. Endpoints `/api/retroarch/follow`, `/bios`, `/bios/apply`.


# Amendment 29 - Sort a mixed folder (v0.2)

`romorg/sortroot.py`: `plan_sort` (identified files to their system's folder; loose unmatched files to `<aside>/_unmatched`; non-ROM
files to `<aside>/_other`; since Amendment 30 a save or note beside a ROM is such a file and no longer follows it), `plan_sweep` / `plan_restore` (the reserved folders of a library build to and
from `<aside>/<system folder>/`), `apply_moves` / `undo_moves` (journal, never overwrite, emptied folders removed). Server:
`/api/collection/sort/plan|apply|undo`, `/api/collection/aside/restore`; in place collection builds sweep after applying.


# Amendment 30 - Saves are part of the build (v0.2)

**What.** RetroArch saves and save states are looked after by Build library (every system) and by Collection, where RetroArch keeps
them (`savefile_directory` / `savestate_directory`, one folder per core when `sort_savefiles_enable`), never next to a ROM. Before,
saves only followed ROM *renames* (Amendment 28); a ROM that the rules archived left its saves behind with nothing to belong to.

**Why, measured.** The user's SNES folder (4,083 files, default rules, saves in `F:\Emulation\assets\saves\<core>\`): of 9 save sets
in the two SNES cores (`bsnes`, `Snes9x`; 60 files in scope) 3 belong to ROMs the rules keep, 4 to ROMs the rules archive (Super Mario
World (USA): the rules keep Europe Rev 1; Zelda ALttP Switch Online and Yoshi's Island (Europe) (En,Fr,De): superseded; Star Fox
(Japan): excluded; together 12 files, 8 of them states) and 2 (Super Mario Collection (Japan) (Rev 1), Yoshi's Island (USA, Asia)
(Rev 1)) to no ROM in the folder at all (no option can help those). With **leave** the 4 are orphaned (today's behaviour); with
**keep** their 4 ROMs stay (kept 2,266 instead of 2,262; excluded 853, superseded 924); with **archive** the ROMs go and the 12 files
go with them. Dry run on the real folders through the real API, nothing applied.

**The rule.** `LibraryProfile.saved_games` = `"keep"` (default) | `"archive"` | `"leave"`, validated in the constructor and in
`from_dict` (a bad value, or a profile saved before the field, takes the base profile's value, so `keep`), written by `to_dict`; per
system and as a global rule of Collection (`collection.GLOBAL_KEYS`). `totals.profile_signature` leaves it out (the totals do not
look at saves); the server's `plan_id` includes it.

* `keep`: `library.select(items, profile, platform, ..., saved=frozenset)` calls `apply_saved` before `apply_overrides`: an item
  the rules made `excluded` or `superseded` (not part of a multi-disk set, not a symlink; `incomplete` is left alone) whose content
  name (`Item.path.stem`, and the stem of the archive member) is in `saved` is kept with `Decision.codes == ("saved_keep",)` and the
  reason "kept: you have saves or save states for it". A user's own "always exclude" still wins. The decision is made before
  `_vanished`, so a title kept for its saves does not vanish. `Selection.saved_kept()` counts them; `organiser.reason_counts` has
  `kept_saved`. Both plan builders (`organiser._plan_core`, `discsys.plan_units`) pass `saved` through.
* `saved` comes from `App._make_library_plan`: `retroarch.list_saves(install, platform)` walks the save roots ONCE per plan (the
  files stay on the plan as `plan.save_files`) and `retroarch.save_name_keys` turns the file names into every content name they
  could be a save of (each dot-cut whose rest has no space or bracket; `fold_name`d, so case-insensitive on Windows only). With saves
  sorted by core only the folders of the cores that play the platform count (`cores_for_platform` over `core_infos(...,
  firmware_only=False)`; the folder name is the core's `corename`); a folder that is no core's name, and files in the root, count
  for every system. Empty when no RetroArch install is selected or it keeps its saves beside the games.
* `archive`: `App._saves_moves` finds the saves of the games the plan sets aside (`code` set and status `move`; a name that a file
  that stays also has is not set aside) with `retroarch.saves_of` (`SaveMatcher`: the longest game name wins; `Dr. Mario` is not cut
  at its dot) and plans `sortroot.SMove`s to `<archive>/<system folder name>/_saves/<path below the save root>` (`<ROM
  folder>/_saves/...` while no archive folder is used). `folders.SAVES_DIR` is a reserved folder and `sortroot.ASIDE_FOLDERS`
  includes it, so a sweep moves it out with the other reserved folders and `plan_restore` brings it back. `sortroot.Names` never
  overwrites (the name gets ` (2)`). The moves are journalled like a sweep (`apply_moves(kind="libsweep", library_log=...)`), so the
  library's Undo (and the Collection's "Undo last", the saves being part of its `sort` journal) puts ROM and saves back together.
  Skipped, with a note, while RetroArch runs (`saves_archived.skipped_running`); the ROMs are archived anyway. In Collection (in
  place) the moves are made in the same pass and the same journal as the other moves into the archive: no file moves twice.
* `leave`: nothing new.
* Rename-follow (Amendment 28) is unchanged but looks only at the files of the system's cores (`plan_follow(files=...)`); the
  archive step runs first, so the saves of an archived and renamed game are archived under the name they have.
* Building into another folder (`elsewhere`): nothing is archived from the user's folders and the saves stay; `keep` decides which
  games get copied; saves of renamed games are copied as before.

**Preview.** `/api/library/plan` has `saves`: `{found, mode, follow, files, kept, running, elsewhere, rename: {files, games,
conflicts}, archive: {files, games, states, to}}` (a dry run of `plan_follow` on the planned renames and of the archive moves) and
`reasons.kept_saved`; the Collection preview has the same per system as `saves_plan`. The counts equal what the build then does
(tests). The apply answers carry `saves` (renames), `saves_archived` and, for the Collection, `saves_archived` at the top.

**Sort (Amendment 29 changed).** `plan_sort` no longer moves a loose file that sits beside a ROM (a save, a note) into the system
folder with it, nor leaves one beside a ROM that stays: it is not a ROM and goes to `<archive>/_other/<same relative path>`. In a
disc game's own folder everything stays (the Redump `.zip`, `.md5`, `.cue`, `.gdi` the verification uses) except the emulator saves
and states of that game, recognised by `retroarch.is_save_of(name, image stem)` (`.srm .sav .state .state<N> .state.auto
.state<N>.png .mcr .mcd .eep .sra .fla .mpk .nvr .rtc .uss`, `.<N>.srm`, Flycast `.A1.bin`..`.D6.bin`): `sortroot.unit_saves`. The
disc scan counts every file that starts with the image's name as part of the game, so the virtual disc state of the Collection
plan (`collection.virtual_disc`) leaves the saves out, and `SMove.leave` keeps a moving folder from taking them along.

**Bugs found on the way.** (1) A Build library whose only changes are archive moves (nothing renamed inside the folder) wrote no
undo log, so there was nothing to Undo it with: `organiser.write_marker_log` now makes a log with no steps that the sweep journals
link to. (2) `retroarch.match_save` compared names exactly on Windows (RetroArch there ignores case); it folds case on Windows
only now. (3) Renaming saves looked at the folders of every core, so a Genesis save could follow a same-named SNES ROM.

**Not done.** Copying a save to the edition that replaces the game (rejected: unsafe). Saves of a multi-disc `.m3u` game (they are
named after the playlist, not after a disc: not matched). Saves of ROMs already sitting in `_excluded/` or `_superseded/` from an
earlier build (only the games a build moves now). `incomplete` sets and the disks of a TOSEC set are left to their rules, `keep`
does not protect a `_duplicates/` copy (the keeper's own saves are untouched; the spare's go with it in `archive` mode). The "have
N of M" totals do not look at saves. Saves are found by name only.


## v0.2: removed and changed

* `POST /api/organise/plan` and `/api/organise/apply` are gone (nothing in the UI called them; Build library does the same job with
  the rules). `/api/organise/undo-logs` and `/api/organise/undo` stay: they undo any `.romorg-undo-*.json`.
* Collection (in place) moves each file once: `_collection_single_pass` merges the sort into the system folder with the library
  plan's own moves, and sends what the rules archive straight to the archive folder (journalled by `sortroot`, as it lies
  outside the ROM folder). "Convert first" converts from where the raw files lie (`_collection_convert_first`): the new file is
  written in the system's folder and the original goes to that folder's `_converted_originals`; the folder is then read again
  and planned as usual. The undo logs of a collection build live in the ROM root.


## Windows pass 4 (2026-10-08): v0.2 on Windows

Answer to `docs/HANDOVER_WINDOWS_4.md`. v0.2 had only run on the Steam Deck; this pass ran it on Windows 11 (Python 3.14, 16
threads), fixed what broke, and fixed the bugs it found on the way, several of which exist on Linux too.

**Numbers, all on commit 648888b.** Tests: 1358 (1238 before the pass). Windows: OK, 58 skipped. WSL Ubuntu 24.04 (Python 3.12):
OK, 96 skipped. The AppImage's own Python 3.13 in an Arch container without system libzstd / libFLAC, read-only root, uid 1000,
8 CPUs, no network: OK twice, 97 skipped; the AppImage (20.6 MB) passes `--self-check` through a FUSE mount and
`packaging/smoke_test.sh`. Windows packages: zip 14.6 MB, exe 12.5 MB, both pass the self-check and the new smoke test.
The browser tests (30) run on Windows with Edge. Not run: a real Steam Deck.

**What was wrong on Windows only**

* Three RetroArch tests expected `~/...` in `retroarch.cfg`; on Windows the app rightly writes absolute paths (RetroArch there
  does not expand `~`). The tests now state the form per platform.
* RetroArch was not found unless it lived in `%APPDATA%`, under Program Files' Steam or one of four fixed folders: a Chocolatey
  install (`C:\tools\RetroArch-Win64`) was missed. Detection now also reads the Steam path and the installer's uninstall key from
  the registry, every Steam library, PATH, Chocolatey, Scoop, the usual unpack places and the root of each fixed drive, and the
  folder of a running `retroarch.exe` (`retroarch.WinSystem`; nothing outside `home` / `env` is read when a caller passes them).
  `ROMORG_RETROARCH_DETECT=0` switches machine detection off; every test module that starts the server sets it.
* `is_running` started `tasklist` (a console window from the windowed exe, 165 ms, localised text): now a Toolhelp snapshot
  through ctypes (16 ms). Folders inside a portable install are written `:\saves`, as RetroArch does.
* A root that already had `SNES\` crashed the whole Collection job once a rule set a file aside (paths compared as text); a
  rename that only changes case was dropped; sync deleted a file whose name only changed case (`collection.standard_folder`,
  `libexport`).
* Read-only files stayed under both names after a move (link + unlink), and emptied read-only folders stayed.
* A ROM folder at the top of a drive: `GET /api/collection` answered 500 and overlap checks were wrong.
* The build scripts' smoke test only asked for one URL: `packaging/smoke_test.ps1` now asserts what `smoke_test.sh` does (a
  test compares the two scripts), plus that Quit leaves no process. The exe build no longer touches the builder's own data.
* Windows showed `~/Emulation/roms` example paths; two RetroArch folder fields had no native Browse button.

**What was wrong everywhere (found here)**

* A move that could not rename fell back to copy-and-delete for any reason. A file another program holds open was copied and
  could not be deleted: two copies, a failed move, no undo record. Now (`sortroot.move_path`, `libexport._transfer`,
  `retroarch._move`): rename; copy only to another device; the copy is removed if the original cannot be. Sharing violations are
  retried on Windows (3 x 100 ms, in `sortroot` only).
* The sort journal was written once at the end, so a killed run lost the record of every move made: now a line before each move.
* Undo removed every empty folder above the archive, including the user's own folder holding it; folders that were empty before
  a build were removed and not restored; "Undo last" forgot a build whose files could not all be moved back; a build stopped
  after "Convert first" left nothing to undo; undo and restore could run while a job was moving files.
* `retroarch.cfg`: line endings were rewritten, a BOM hid the first key, two changes in one second overwrote the first backup,
  undo threw away what RetroArch had saved since, a save dated before 1980 killed the zip backup.
* BIOS check: no core matched the SNES, PSP cores were listed for PlayStation, a firmware entry that is a folder was always
  "missing". Several server handlers answered 500 on a wrong type.
* Folder dialog: two clicks opened two dialogs; a dialog stayed open after Quit. UI: a double click started two jobs.
* The self-check now fails when a module or a file of the web UI is missing from a package (`check_app`); `smoke_test.sh`
  checks that line and the v0.2 pages (`view-collection`, `view-retroarch`, `view-chd`, `/api/collection`, `/api/retroarch`).

**Checked by hand through the HTTP API on real folders:** the handover's mixed folder (spaces, apostrophes, `&`, `%`, Japanese,
accents; a duplicate, an unknown ROM, a picture): 20 moves, no path both a source and a destination, the `.romorg-undo-*.json`
files skipped on a rescan, undo exact; a headered `.smc` with *Convert first*; archive and *Build elsewhere* (Copy, Move, keep in
sync, cancel then undo) on another physical drive; RetroArch save moves across drives against a copy of a real install
(291 core info files parse); the built exe in headless Edge (home, 18 systems x 3 tabs, the three new pages: no JavaScript error,
no failed request, no overflow from 380 to 1280 px); the native folder dialog (appears in front, non-ASCII path, cancel, server
not blocked); the progress meter over 4.3 GB (monotonic, sane ETA).

**Left as they are (design questions for the user, both platforms)**

* A save next to a ROM follows it into the system folder but is not renamed with it; the next build sends it to `_other`.
* A library build that only archives (nothing renamed) has no undo in the UI (`undo_log` is `None`).
* A "build elsewhere" Move records its manifest every 100 files: a killed run can leave up to 99 moved files unrecorded (safe in
  the destination, not undoable). Per-file recording costs speed on SD cards.
* *Convert first* converts a headered file lying in `_converted_originals` again when its clean copy is gone.
* `organiser.apply_renames` has no sharing-violation retry (the handover asked for it in `sortroot` only).

**For the Deck:** shared code changed (`sortroot`, `libexport`, `organiser`, `collection`, `retroarch`, `server`, `app.js`,
`selfcheck`, `smoke_test.sh`). Run `docs/DECK_RUNBOOK.md` again from step 1, and on the RetroArch page check that the Deck's
installs are still found, that changing a folder changes only that line of `retroarch.cfg`, and that Undo returns it.
Not exercised anywhere for real: the registry uninstall key, Scoop, a Steam RetroArch on Windows, RetroBat / LaunchBox /
EmuDeck layouts (those paths only count when a `retroarch.cfg` is there), a running RetroArch, paths over 260 characters.
