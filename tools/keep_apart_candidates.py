"""List Amiga titles that may be two different games (candidates for ``library.KEEP_APART``).

"Combine alternate titles" makes the releases of one TOSEC title by different publishers one game. A few titles are two different
games; LaunchBox knows some of them as two entries ("Arena [Psygnosis]", "Arena [Defcom]"), but misses most and also lists some
single games twice (Overdrive: developer and publisher), so its answer is a lead to check by hand, never a rule.

    python3 tools/keep_apart_candidates.py Metadata.zip

``Metadata.zip``: https://gamesdb.launchbox-app.com/Metadata.zip (about 108 MB). Prints the TOSEC titles of the installed Amiga Games
DAT that have releases by more than one publisher and two or more LaunchBox Amiga games, minus those already in ``KEEP_APART``.
"""

from __future__ import annotations

import collections
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from romorg import alttitles, library, platforms, tags  # noqa: E402


def launchbox_amiga(zip_path: str) -> dict[str, list[tuple[str, str, str]]]:
    """``{normal title: [(name, year, publisher), ...]}`` of LaunchBox's Amiga games (rated or not)."""
    out: dict[str, list[tuple[str, str, str]]] = collections.defaultdict(list)
    with zipfile.ZipFile(zip_path) as zf, zf.open("Metadata.xml") as fh:
        root = None
        for ev, el in ET.iterparse(fh, events=("start", "end")):
            if ev == "start":
                root = root if root is not None else el
                continue
            if el.tag == "Game":
                if (el.findtext("Platform") or "") == "Commodore Amiga":
                    name = el.findtext("Name") or ""
                    row = (name, (el.findtext("ReleaseDate") or el.findtext("ReleaseYear") or "")[:4], el.findtext("Publisher") or "")
                    out[alttitles.node(name.split("[")[0])].append(row)
                root.clear()
            elif el.tag in ("GameAlternateName", "GameImage", "Emulator", "EmulatorPlatform", "PlatformAlternateName"):
                root.clear()
    return out


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    lb = launchbox_amiga(argv[1])
    p = platforms.get_platform("Commodore Amiga")
    dats, _missing = platforms.load_platform_dats(p)
    pubs: dict[str, set[str]] = collections.defaultdict(set)
    for d in dats:
        if "Games" not in d.name:
            continue
        for r in d.roms:
            t = tags.parse_name(r.name, tags.STYLE_TOSEC)
            pubs[t.title.casefold().strip()].add(t.publisher)
    found = 0
    for title, ps in sorted(pubs.items()):
        games = lb.get(alttitles.node(title), [])
        if len(ps) < 2 or len(games) < 2 or title in library.KEEP_APART:
            continue
        found += 1
        print(f"{title}\n    DAT publishers: {', '.join(sorted(ps))}")
        for name, year, pub in games:
            print(f"    LaunchBox: {name} ({year}, {pub})")
    print(f"{found} candidate(s); check each by hand before adding it to library.KEEP_APART")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
