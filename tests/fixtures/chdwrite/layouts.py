"""The disc layouts the CHD writer is checked on: small generated sets (deterministic, from ``chdtestlib``).

``chdman_reference.json`` holds what chdman 0.289 wrote for each of them (header SHA-1, data SHA-1, metadata, size);
``python3 tests/fixtures/chdwrite/layouts.py /path/to/chdman`` regenerates it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent))
sys.path.insert(0, str(HERE.parent.parent.parent))

CUES = {
    "redump": 'FILE "m2.bin" BINARY\n  TRACK 01 MODE2/2352\n    INDEX 01 00:00:00\nFILE "a1.bin" BINARY\n  TRACK 02 AUDIO\n'
              '    INDEX 00 00:00:00\n    INDEX 01 00:00:10\nFILE "a2.bin" BINARY\n  TRACK 03 AUDIO\n    INDEX 00 00:00:00\n'
              '    INDEX 01 00:00:07\n',
    "noidx0": 'FILE "d1.bin" BINARY\n  TRACK 01 MODE1/2352\n    INDEX 01 00:00:00\nFILE "a1.bin" BINARY\n  TRACK 02 AUDIO\n'
              '    INDEX 01 00:00:00\nFILE "a3.bin" BINARY\n  TRACK 03 AUDIO\n    INDEX 01 00:00:00\n',
    "single": 'FILE "single.bin" BINARY\n  TRACK 01 MODE1/2352\n    INDEX 01 00:00:00\n  TRACK 02 AUDIO\n    INDEX 00 00:00:30\n'
              '    INDEX 01 00:00:35\n  TRACK 03 AUDIO\n    INDEX 01 00:00:57\n',
    "pregap": 'FILE "d1.bin" BINARY\n  TRACK 01 MODE1/2352\n    INDEX 01 00:00:00\nFILE "a1.bin" BINARY\n  TRACK 02 AUDIO\n'
              '    PREGAP 00:02:00\n    INDEX 01 00:00:00\n',
    "postgap": 'FILE "d1.bin" BINARY\n  TRACK 01 MODE1/2352\n    INDEX 01 00:00:00\n    POSTGAP 00:02:00\nFILE "a1.bin" BINARY\n'
               '  TRACK 02 AUDIO\n    INDEX 00 00:00:00\n    INDEX 01 00:00:10\n',
    "cooked": 'FILE "cooked.bin" BINARY\n  TRACK 01 MODE1/2048\n    INDEX 01 00:00:00\n',
    "m2336": 'FILE "m2336.bin" BINARY\n  TRACK 01 MODE2/2336\n    INDEX 01 00:00:00\n',
    "audio_only": 'FILE "a1.bin" BINARY\n  TRACK 01 AUDIO\n    INDEX 01 00:00:00\nFILE "a2.bin" BINARY\n  TRACK 02 AUDIO\n'
                  '    INDEX 00 00:00:00\n    INDEX 01 00:00:05\n',
    "idx2": 'FILE "d1.bin" BINARY\n  TRACK 01 MODE1/2352\n    INDEX 01 00:00:00\nFILE "a1.bin" BINARY\n  TRACK 02 AUDIO\n'
            '    INDEX 00 00:00:00\n    INDEX 01 00:00:10\n    INDEX 02 00:00:20\n',
    "two_data": 'REM a comment\nFILE "d1.bin" BINARY\n  TRACK 01 MODE1/2352\n    FLAGS DCP\n    INDEX 01 00:00:00\n'
                'FILE "m2.bin" BINARY\n  TRACK 02 MODE2/2352\n    INDEX 00 00:00:00\n    INDEX 01 00:00:04\n',
    "spaces": 'FILE "track one (data).bin" BINARY\n  TRACK 01 MODE1/2352\n    INDEX 01 00:00:00\n',
}
GDIS = {
    "gd": '3\n1 0 4 2352 d1.bin 0\n2 600 0 2352 a1.raw 0\n3 45000 4 2352 m2x.bin 0\n',
    "gd5": '5\n1 0 4 2352 d1.bin 0\n2 756 0 2352 a1.raw 0\n3 45000 4 2352 m2x.bin 0\n4 45180 0 2352 a2.raw 0\n'
           '5 45400 4 2352 d1b.bin 0\n',
    "gdq": '3\n1 0 4 2352 "d1.bin" 0\n2 600 0 2352 "a1.raw" 0\n3 45000 4 2048 "x.iso" 0\n',
}
ISOS = {"iso_cd": "createcd", "iso_dvd": "createdvd"}
NAMES = list(CUES) + list(GDIS) + list(ISOS)


def write_inputs(folder) -> None:
    """Every track file and sheet of every layout, into ``folder``."""
    import chdtestlib as T
    folder = Path(folder)
    d1, m2 = T.make_data_track(30, 1), T.make_mode2_track(30, 2)
    a1, a2, a3 = T.make_audio_track(27, 3), T.make_audio_track(21, 4), T.make_audio_track(13, 5)
    iso = T.make_iso(37, 6)
    files = {"d1.bin": d1, "m2.bin": m2, "a1.bin": a1, "a2.bin": a2, "a3.bin": a3, "x.iso": iso, "cooked.bin": iso,
             "single.bin": d1 + a1 + a2, "m2336.bin": b"".join(m2[i * 2352 + 16:(i + 1) * 2352] for i in range(30)),
             "a1.raw": a1, "a2.raw": a2, "m2x.bin": T.make_data_track(26, 9, 45000),
             "d1b.bin": T.make_data_track(11, 10, 45400), "track one (data).bin": d1}
    for name, data in files.items():
        (folder / name).write_bytes(data)
    for name, text in CUES.items():
        (folder / f"{name}.cue").write_text(text)
    for name, text in GDIS.items():
        (folder / f"{name}.gdi").write_text(text)


def source(folder, name: str):
    """``(input path, mode)`` of a layout."""
    folder = Path(folder)
    if name in ISOS:
        return folder / "x.iso", ISOS[name]
    return folder / (name + (".gdi" if name in GDIS else ".cue")), "createcd"


def main(chdman: str) -> None:
    from romorg import chd
    out = {}
    with tempfile.TemporaryDirectory() as tmp:
        write_inputs(tmp)
        for name in NAMES:
            src, mode = source(tmp, name)
            target = os.path.join(tmp, name + ".chd")
            subprocess.run([chdman, mode, "-f", "-i", str(src), "-o", target], check=True, capture_output=True)
            with chd.Chd(target, load_map=False) as c:
                out[name] = {"sha1": c.sha1, "raw_sha1": c.raw_sha1, "logical_bytes": c.logical_bytes,
                             "hunk_bytes": c.hunk_bytes, "unit_bytes": c.unit_bytes, "size": os.path.getsize(target),
                             "metadata": [[tag.decode("latin-1"), body.decode("latin-1")] for tag, body in c.metadata]}
    (HERE / "chdman_reference.json").write_text(json.dumps(out, indent=1, sort_keys=True) + "\n")
    print(f"{len(out)} layouts written to chdman_reference.json")


if __name__ == "__main__":
    main(sys.argv[1])
