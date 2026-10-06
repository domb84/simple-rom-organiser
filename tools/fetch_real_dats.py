"""Fetch the real DATs the "real data" tests want, with the app's own downloaders, and lay them out for the tests.

Usage:  py -3.14 tools/fetch_real_dats.py <target-folder>

Needs the network. Downloads Redump (Dreamcast, PlayStation, PlayStation 2), No-Intro (libretro mirror), WHDLoad and
the TOSEC pack into a private data dir (<target>/datadir), then arranges <target>/realscratch for the test variables:

    ROMORG_REAL_SCRATCH   = <target>/realscratch      (dats/TOSEC, nointro, whd, pack.zip)
    ROMORG_REAL_SONY_DATS = <target>/realscratch/redump_sony
    ROMORG_REAL_DC_DAT    = <target>/realscratch/redump/Sega - Dreamcast*.dat
    WHD_REAL_DAT          = <target>/realscratch/whd/whd.dat
    (ROMORG_REAL_DC_DIR = a folder of the four Redump-named Dreamcast games, ROMORG_REAL_SETS / _CHDMAN_CHDS: see
     tests/test_chd.py and tests/test_chdwrite.py)

Nothing is written to the repository or to the user's real app data. The one DAT that stays missing is the DAT-o-MATIC
GBA DAT (one test, skipped): it is not offered by any downloader of the app.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1].startswith("-"):
        print(__doc__)
        return 2
    target = Path(argv[1]).absolute()
    data = target / "datadir"
    os.environ["ROMORG_DATA_DIR"] = str(data)
    os.environ.pop("ROMORG_OFFLINE", None)
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from romorg import nointro, paths, redump, tosec, whdload

    failed = False
    for mod in (redump, nointro, whdload):
        res = mod.update_dats(progress=lambda *a: None)
        print(mod.__name__, {k: res[k] for k in ("downloaded", "unchanged", "failed", "count")}, flush=True)
        failed |= bool(res["failed"])
    print("tosec", tosec.update_dats(force=True, keep_pack=True, progress=lambda *a: None), flush=True)

    out = target / "realscratch"
    (out / "dats" / "TOSEC").mkdir(parents=True, exist_ok=True)
    for f in paths.dats_dir().glob("*.dat"):
        shutil.copy2(f, out / "dats" / "TOSEC" / f.name)
    shutil.copytree(paths.nointro_dir(), out / "nointro", dirs_exist_ok=True)
    (out / "whd").mkdir(exist_ok=True)
    shutil.copy2(paths.whdload_dir() / f"{whdload.DAT_NAME}.dat", out / "whd" / "whd.dat")
    for pack in sorted(paths.cache_dir().glob("*.zip"))[-1:]:
        shutil.copy2(pack, out / "pack.zip")
    (out / "redump").mkdir(exist_ok=True)
    (out / "redump_sony").mkdir(exist_ok=True)
    manifest = json.loads((paths.redump_dir() / "manifest.json").read_text(encoding="utf-8"))
    for name, row in manifest.items():
        stamped = f"{name} ({row['version']}).dat"
        shutil.copy2(paths.redump_dir() / f"{name}.dat", out / "redump" / stamped)
        if name.startswith("Sony"):
            # the Sony test globs "Sony - PlayStation - *.dat" / "Sony - PlayStation 2 - *.dat"
            shutil.copy2(paths.redump_dir() / f"{name}.dat", out / "redump_sony" / f"{name} - Datfile ({row['version']}).dat")
    print("laid out in", out)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
