"""A small real Amiga library (synthetic DAT + files) served by the real app, for the browser checks.

``python3 -m tests.uifixture`` prints the URL and keeps serving (handy to poke at the UI by hand)."""
from __future__ import annotations

import hashlib
import os
import random
import shutil
import sys
import tempfile
import threading
import zlib
from pathlib import Path
os.environ.setdefault("ROMORG_RETROARCH_DETECT", "0")      # never pick up a RetroArch installed on this machine

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

GAMES = "Commodore Amiga - Games - [ADF]"
# (name, rating-less TOSEC names: title, year, versions, flags) - one file each unless noted
GAME_NAMES = [
    "Alpha Quest v1.0 (1990)(Acme)",
    "Alpha Quest v1.1 (1991)(Acme)",
    "Beta Blaster (1992)(Bits)",
    "Gamma Run (1989)(Cora)",
    "Gamma Run (1989)(Cora)[cr FLT]",
    "Delta Force (1993)(Dune)[b]",
    "Epsilon (1994)(Echo)(Disk 1 of 2)",
    "Epsilon (1994)(Echo)(Disk 2 of 2)",
    "Zeta Zone (1995)(Zed)",
    "Eta Escape (1996)(Echo)(de)",
]
NOT_OWNED = ["Theta Tower (1997)(Thor)", "Iota Island (1998)(Ink)"]
GBA_DAT = "Nintendo - Game Boy Advance"
GBA_OWNED = ["Alpha Run (USA)", "Alpha Run (Europe)", "Beta Bash (USA)", "Gamma Gear (Japan)", "Delta Dash (USA) (Beta)"]
GBA_MISSING = ["Epsilon Edge (USA)", "Zeta Zap (Europe)"]


class Fixture:
    def __init__(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="romorg-ui-")).resolve()
        self.data = self.tmp / "data"
        self.root = self.tmp / "amiga"
        self.root.mkdir()
        (self.data / "dats").mkdir(parents=True)
        self._env = {"ROMORG_DATA_DIR": str(self.data), "ROMORG_OFFLINE": "1"}
        self._old = {k: os.environ.get(k) for k in self._env}
        os.environ.update(self._env)
        rng = random.Random(7)
        lines = ['<?xml version="1.0"?>', "<datafile>",
                 f"<header><name>{GAMES}</name><description>{GAMES}</description><version>2025-01-30</version></header>"]
        for name in GAME_NAMES + NOT_OWNED:
            data = bytes(rng.getrandbits(8) for _ in range(1024))
            lines.append(f'<game name="{name}"><description>{name}</description>'
                         f'<rom name="{name}.adf" size="{len(data)}" crc="{zlib.crc32(data):08x}" '
                         f'md5="{hashlib.md5(data).hexdigest()}" sha1="{hashlib.sha1(data).hexdigest()}"/></game>')
            if name in GAME_NAMES:
                (self.root / f"{name}.adf").write_bytes(data)
        lines.append("</datafile>")
        (self.data / "dats" / f"{GAMES} (TOSEC-v2025-01-30_CM).dat").write_text("\n".join(lines))
        # a No-Intro system too (the Games list, ratings columns): one DAT, most of it owned
        self.gba = self.tmp / "gba"
        self.gba.mkdir()
        (self.data / "nointro").mkdir()
        out = [f'clrmamepro (\n\tname "{GBA_DAT}"\n\tdescription "{GBA_DAT}"\n\tversion "20250101-000000"\n)\n']
        for name in GBA_OWNED + GBA_MISSING:
            data = bytes(rng.getrandbits(8) for _ in range(2048 if "Beta" in name else 4096))
            out.append(f'game (\n\tname "{name}"\n\tdescription "{name}"\n\trom ( name "{name}.gba" size {len(data)} '
                       f'crc {zlib.crc32(data):08x} md5 {hashlib.md5(data).hexdigest()} sha1 {hashlib.sha1(data).hexdigest()} )\n)\n')
            if name in GBA_OWNED:
                (self.gba / f"{name}.gba").write_bytes(data)
        (self.data / "nointro" / f"{GBA_DAT}.dat").write_text("\n".join(out))
        from romorg import server
        self.server = server.make_server("127.0.0.1", 0, token="t", auto_update=False)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.port}/"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)


if __name__ == "__main__":
    fx = Fixture()
    print(fx.url, flush=True)
    threading.Event().wait()
