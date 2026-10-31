#!/usr/bin/env python3
"""Change (or add) one set's result in the shipped list ``romorg/whdknown.json.gz``, the format kept byte for byte (compact JSON, key
order, gzip level 9 with the name ``whdknown.json`` and mtime 0: loading and writing back unchanged data gives the identical file).

    tools/whdknown_set.py LEVEL NAME [NAME ...]                 the set made of exactly these disks (TOSEC names, or an SPS release's
                                                                "<release> - <file>.ipf" images, in the list's order) gets LEVEL
    tools/whdknown_set.py LEVEL NAME ... --archive X.lha --zip RELEASE.zip
                                                                a set the list does not have yet: added with its install and the
                                                                checksums (first 16 hex of SHA-1) of the images in the zip

Without ``--archive`` the set must exist and keeps its install. Only the level changes. Prints what changed; ``--dry-run`` writes nothing.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import sys
import zipfile
from pathlib import Path

LIST = Path(__file__).resolve().parent.parent / "romorg" / "whdknown.json.gz"


def load(path: Path = LIST) -> tuple[dict, str]:
    raw = path.read_bytes()
    name = raw[10:raw.index(b"\0", 10)].decode() if raw[3] & 8 else "whdknown.json"
    return json.loads(gzip.decompress(raw)), name


def dump(data: dict, name: str = "whdknown.json") -> bytes:
    buf = io.BytesIO()
    with gzip.GzipFile(filename=name, fileobj=buf, mode="wb", compresslevel=9, mtime=0) as g:
        g.write(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    return buf.getvalue()


def set_level(data: dict, names: list[str], level: int, archive: str = "", zip_path: str = "") -> str:
    key = "\n".join(names)
    sets = data["sets"]
    if key in sets:
        was = sets[key][0]
        sets[key][0] = level
        return f"{names[0]} ...: {was} -> {level}"
    if not archive:
        raise SystemExit(f"no such set in the list (add it with --archive): {names[0]}")
    sets[key] = [level, archive]
    if zip_path:
        z = zipfile.ZipFile(zip_path)
        for n in names:
            member = n.rsplit(" - ", 1)[1]
            data["sha1"][n] = hashlib.sha1(z.read(member)).hexdigest()[:16]
    return f"{names[0]} ...: added at {level} ({archive})"


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("level", type=int)
    ap.add_argument("names", nargs="+")
    ap.add_argument("--archive", default="")
    ap.add_argument("--zip", default="")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    data, name = load()
    print(set_level(data, a.names, a.level, a.archive, a.zip))
    if not a.dry_run:
        LIST.write_bytes(dump(data, name))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
