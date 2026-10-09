"""Wii / GameCube (Dolphin) and Wii U (Cemu): discs told by their headers, the saves of both emulators, GameTDB names, the pages' API.
Synthetic files only."""

from __future__ import annotations

import gzip
import json
import os
import shutil
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sys
sys.path.insert(0, os.path.dirname(__file__))
import rvztestlib as R  # noqa: E402
from romorg import gametdb, nintendoapp, nintendodisc, nintendosaves


def disc_header(game_id: str, name: str, wii: bool, disc: int = 0, rev: int = 0) -> bytes:
    h = bytearray(0x80)
    h[:6] = game_id.encode()
    h[6], h[7] = disc, rev
    struct.pack_into(">I", h, 0x18, 0x5D1C9EA3 if wii else 0)
    struct.pack_into(">I", h, 0x1C, 0 if wii else 0xC2339F3D)
    h[0x20:0x20 + len(name)] = name.encode()
    return bytes(h)


def iso(game_id: str, name: str, wii: bool = True) -> bytes:
    return disc_header(game_id, name, wii) + bytes(0x400)


def rvz(game_id: str, name: str, wii: bool = True) -> bytes:
    return b"RVZ\x01" + bytes(0x54) + disc_header(game_id, name, wii) + bytes(0x100)


def wbfs(game_id: str, name: str) -> bytes:
    return b"WBFS" + bytes(0x1FC) + disc_header(game_id, name, True) + bytes(0x100)


def wux(code: str, region: str = "USA", rev: int = 0) -> bytes:
    """A WUX: header, a sector table of one sector, and that sector (the WUD's first), at 0x8000."""
    sector = 0x8000
    head = b"WUX0" + struct.pack("<I", 0x1099D02E) + struct.pack("<I", sector) + bytes(4) + struct.pack("<Q", sector) + bytes(8)
    table = struct.pack("<I", 0)
    product = f"WUP-P-{code}-{rev:02d}-551{region}-0".encode()
    return head + table + bytes(sector - len(head) - len(table)) + product + bytes(sector - len(product))


def ps2_iso(serial_file: str = "SLUS_209.46", boot: str = "BOOT2") -> bytes:
    """A tiny ISO 9660 image with a SYSTEM.CNF: PVD at sector 16, the root directory at 18, the file at 19."""
    sector = 2048
    img = bytearray(sector * 20)
    pvd = bytearray(sector)
    pvd[0], pvd[1:6], pvd[6] = 1, b"CD001", 1
    root = bytearray(34)
    root[0], root[25], root[32] = 34, 2, 1
    struct.pack_into("<I", root, 2, 18)
    struct.pack_into("<I", root, 10, sector)
    pvd[156:190] = root
    img[16 * sector:17 * sector] = pvd
    cnf = f"{boot} = cdrom0:\\{serial_file};1\r\nVER = 1.00\r\nVMODE = NTSC\r\n".encode()
    name = b"SYSTEM.CNF;1"
    rec = bytearray(33 + len(name))
    rec[0], rec[32] = len(rec), len(name)
    rec[33:] = name
    struct.pack_into("<I", rec, 2, 19)
    struct.pack_into("<I", rec, 10, len(cnf))
    img[18 * sector:18 * sector + len(rec)] = rec
    img[19 * sector:19 * sector + len(cnf)] = cnf
    return bytes(img)


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="romorg-nin-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def put(self, rel: str, data: bytes) -> Path:
        p = self.tmp / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return p


class Discs(Base):
    def test_wii_and_gamecube_headers_in_every_container(self) -> None:
        for name, data in (("a.iso", iso("SB4E01", "SMG2")), ("b.rvz", rvz("SB4E01", "SMG2")), ("c.wbfs", wbfs("SB4E01", "SMG2")),
                           ("d.wia", b"WIA\x01" + bytes(0x54) + disc_header("SB4E01", "SMG2", True))):
            info = nintendodisc.read_disc(self.put(name, data))
            self.assertEqual((info.kind, info.game_id, info.name, info.region, info.code4), ("wii", "SB4E01", "SMG2", "USA", "SB4E"), name)
        gc = nintendodisc.read_disc(self.put("g.rvz", rvz("GMSE01", "Sunshine", wii=False)))
        self.assertEqual((gc.kind, gc.container), ("gc", "rvz"))

    def test_what_is_no_disc_is_refused(self) -> None:
        self.assertIsNone(nintendodisc.read_disc(self.put("x.iso", b"nothing here" * 20)))
        self.assertIsNone(nintendodisc.read_disc(self.put("y.iso", disc_header("SB4E01", "x", True)[:20])))

    def test_wii_u_product_code_from_a_wux(self) -> None:
        info = nintendodisc.read_wiiu(self.put("w.wux", wux("AFXE")))
        self.assertEqual((info.kind, info.game_id, info.region, info.container, info.product), ("wiiu", "AFXE", "USA", "wux", "WUP-P-AFXE-00-551USA-0"))
        raw = self.put("r.wud", b"WUP-P-AZAE-02-550USA-0" + bytes(0x100))
        self.assertEqual(nintendodisc.read_wiiu(raw).revision, 2)
        self.assertIsNone(nintendodisc.read_wiiu(self.put("n.wux", b"WUX0" + bytes(0x100))))


def dolphin_tree(root: Path) -> None:
    (root / "Wii" / "title" / "00010000" / "524d4745" / "data").mkdir(parents=True)
    (root / "Wii" / "title" / "00010000" / "524d4745" / "data" / "GameData.bin").write_bytes(b"1" * 100)
    (root / "Wii" / "title" / "00010000" / "524d4745" / "content").mkdir()
    (root / "Wii" / "title" / "00010000" / "524d4745" / "content" / "title.tmd").write_bytes(b"t")
    (root / "Wii" / "title" / "00010000" / "53423445" / "data").mkdir(parents=True)                       # no files: no save
    card = root / "GC" / "USA" / "Card A"
    card.mkdir(parents=True)
    head = b"GMSE" + b"01" + bytes([0xFF, 0]) + b"super_mario_sunshine".ljust(32, b"\0") + bytes(0x100)
    (card / "01-GMSE-super_mario_sunshine.gci").write_bytes(head)
    (card / "01-GMSE-old.gci.deleted").write_bytes(head)
    (root / "GC" / "EUR").mkdir()
    (root / "GC" / "EUR" / "MemoryCardA.EUR.raw").write_bytes(bytes(2048))


class WiiPlatform(Base):
    """The Wii platform: a plain .iso is hashed; Dolphin's compressed formats are told by the game ID in the header."""

    def test_discs_are_matched_by_checksum_or_by_the_header(self) -> None:
        import hashlib
        import zlib
        from romorg import paths, platforms, scanner
        env = mock.patch.dict(os.environ, {"ROMORG_DATA_DIR": str(self.tmp / "data"), "ROMORG_OFFLINE": "1"})
        env.start()
        self.addCleanup(env.stop)
        exact = iso("RSPE01", "Wii Sports")
        games = [("Wii Sports (USA)", exact, ""), ("Mario Kart Wii (Europe) (En,Fr,De,Es,It)", None, ""),
                 ("Super Mario Galaxy (USA)", None, ""), ("Super Mario Galaxy (USA) (Rev 1)", None, ""),
                 ("Legend of Zelda, The - Twilight Princess (USA)", None, "")]
        rows = []
        for name, data, _x in games:
            data = data or name.encode().ljust(64)
            rows.append(f'<game name="{name}"><category>Games</category><description>{name}</description>'
                        f'<rom name="{name}.iso" size="{len(data)}" crc="{zlib.crc32(data) & 0xFFFFFFFF:08x}" '
                        f'md5="{hashlib.md5(data).hexdigest()}" sha1="{hashlib.sha1(data).hexdigest()}"/></game>')
        folder = paths.redump_dir()
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "Nintendo - Wii.dat").write_text('<?xml version="1.0"?><datafile><header><name>Nintendo - Wii</name><version>2026-06-15 03-13-28</version>'
                                                 "</header>" + "".join(rows) + "</datafile>", encoding="utf-8")
        root = self.tmp / "wii"
        self.put("wii/Wii Sports.iso", exact)                                              # the original image: its checksum
        def wii_rvz(name: str, game_id: str, title: str, revision: int = 0) -> None:
            data = bytearray(iso(game_id, title))
            data[7] = revision
            (self.tmp / "wii").mkdir(exist_ok=True)
            R.build_rvz(self.tmp / "wii" / name, bytes(data), compression=R.NONE, disc_type=2)      # (a Wii disc: its checksum is out of reach)

        wii_rvz("mk.rvz", "RMCP01", "Mario Kart Wii")                                       # Europe, by its header
        self.put("wii/smg.wbfs", wbfs("RMGE01", "Super Mario Galaxy"))
        wii_rvz("smg1.rvz", "RMGE01", "Super Mario Galaxy")
        wii_rvz("smg rev.rvz", "RMGE01", "Super Mario Galaxy", revision=1)
        wii_rvz("tp.rvz", "RZDE01", "The Legend of Zelda Twilight Princess")
        wii_rvz("unknown.rvz", "ABCE01", "Not In The Dat")
        self.put("wii/trimmed.iso", iso("RSPE01", "Wii Sports") + b"scrubbed")                  # an .iso whose checksum is nobody's
        self.put("wii/notes.txt", b"hello")
        plat = platforms.get_platform("Nintendo Wii")
        dats, missing = platforms.load_platform_dats(plat)
        self.assertEqual(missing, [])
        res = scanner.scan(root, dats, layout=plat.layout, containers=plat.containers, use_cache=False)
        got = {m.entry.path.name: (m.roms[0].game, m.matched_via, m.container) for m in res.matched}
        self.assertEqual(got["Wii Sports.iso"], ("Wii Sports (USA)", "raw", ""))
        self.assertEqual(got["mk.rvz"][0], "Mario Kart Wii (Europe) (En,Fr,De,Es,It)")
        self.assertEqual(got["tp.rvz"][0], "Legend of Zelda, The - Twilight Princess (USA)")
        self.assertEqual(got["smg.wbfs"], ("Super Mario Galaxy (USA)", "container", "id"))
        self.assertEqual(got["smg1.rvz"][0], "Super Mario Galaxy (USA)")
        self.assertEqual(got["smg rev.rvz"][0], "Super Mario Galaxy (USA) (Rev 1)")
        self.assertEqual(got["trimmed.iso"], ("Wii Sports (USA)", "container", "id"))
        self.assertEqual(sorted(e.path.name for e in res.unmatched), ["notes.txt", "unknown.rvz"])


class IdSaves(Base):
    """The saves of an ID-based emulator as save sets of the system's games (``idsaves``)."""

    def test_wii_saves_are_per_game_id_and_the_gamecube_platform_does_not_see_them(self) -> None:
        from romorg import idsaves
        dolphin_tree(self.tmp / "dolphin")
        game = self.put("wii/galaxy.rvz", rvz("RMGE01", "Super Mario Galaxy"))
        owned = [idsaves.Owned("galaxy", ("Nintendo - Wii", "Super Mario Galaxy (USA)", "Super Mario Galaxy"), game)]
        cfg = nintendoapp.normalise({"wii": {"data": str(self.tmp / "dolphin")}})
        sets = {s.key: s for s in idsaves.sets_for("Nintendo Wii", owned, cfg)}
        self.assertEqual(sorted(sets), ["galaxy"])
        self.assertEqual((sets["galaxy"].source, sets["galaxy"].saves, sets["galaxy"].game), ("dolphin", 1, "Super Mario Galaxy (USA)"))
        self.assertEqual([m[0].name for m in sets["galaxy"].members], ["data"])
        self.assertEqual(sorted(s.key for s in idsaves.sets_for("Nintendo GameCube", [], cfg)),
                         ["dolphin:GMSE", "dolphin:card:MemoryCardA.EUR.raw"])

    def test_gamecube_saves_belong_to_the_game_whatever_its_file_is_called(self) -> None:
        from romorg import idsaves
        dolphin_tree(self.tmp / "dolphin")
        game = self.put("gc/anything at all.rvz", rvz("GMSE01", "Sunshine", wii=False))
        other = self.put("gc/Wii game.iso", iso("RMGE01", "Wii game"))                  # (a Wii disc is not a GameCube game)
        owned = [idsaves.Owned("anything at all", ("Nintendo - GameCube", "Super Mario Sunshine (USA)", "Super Mario Sunshine"), game),
                 idsaves.Owned("wii game", ("", "Wii game", ""), other)]
        cfg = nintendoapp.normalise({"wii": {"data": str(self.tmp / "dolphin")}})
        sets = {s.key: s for s in idsaves.sets_for("Nintendo GameCube", owned, cfg)}
        self.assertEqual(sorted(sets), ["anything at all", "dolphin:card:MemoryCardA.EUR.raw"])
        mine = sets["anything at all"]
        self.assertEqual((mine.source, mine.match, mine.game, mine.saves, mine.files, mine.renames), ("dolphin", "rom", "Super Mario Sunshine (USA)", 1, 1, False))
        self.assertEqual([m[0].name for m in mine.members], ["01-GMSE-super_mario_sunshine.gci"])
        self.assertEqual(sets["dolphin:card:MemoryCardA.EUR.raw"].members, [])         # a memory card cannot be split by game
        off = nintendoapp.normalise({"wii": {"data": str(self.tmp / "dolphin"), "off": "1"}})
        self.assertEqual(idsaves.sets_for("Nintendo GameCube", owned, off), [])
        self.assertFalse(idsaves.active("Nintendo GameCube", off))
        self.assertTrue(idsaves.active("Nintendo GameCube", cfg))


class Saves(Base):
    def test_dolphin_wii_and_gamecube_saves(self) -> None:
        dolphin_tree(self.tmp / "dolphin")
        wii = nintendosaves.find_dolphin_wii(self.tmp / "dolphin")
        self.assertEqual([(s.key, s.files, s.bytes) for s in wii], [("RMGE", 1, 100)])
        gc = nintendosaves.find_dolphin_gc(self.tmp / "dolphin")
        self.assertEqual(sorted((s.key, s.kind, s.region) for s in gc), [("", "memory card", "EUR"), ("GMSE", "gci", "USA")])   # (.deleted is the trash)
        self.assertEqual(nintendosaves.read_gci_header(self.tmp / "dolphin" / "GC" / "USA" / "Card A" / "01-GMSE-super_mario_sunshine.gci"),
                         ("GMSE", "01", "super_mario_sunshine"))

    def test_cemu_saves_and_title_list(self) -> None:
        mlc = self.tmp / "cemu" / "mlc01" / "usr" / "save" / "00050000" / "101b0400" / "user"
        (mlc / "80000001").mkdir(parents=True)
        (mlc / "80000001" / "game.sav").write_bytes(b"s" * 10)
        (mlc / "common").mkdir()                                                                                  # empty: not a save
        (self.tmp / "cemu" / "title_list_cache.xml").write_text(
            '<?xml version="1.0"?><title_list><title titleId="00050000101b0400" version="16"><region>2</region><name>Star Fox Zero</name>'
            '<path>/roms/Star Fox Zero.wux</path></title></title_list>')
        saves = nintendosaves.find_cemu(self.tmp / "cemu" / "mlc01")
        self.assertEqual([(s.key, s.kind, s.note, s.files) for s in saves], [("00050000101B0400", "profile", "80000001", 1)])
        self.assertEqual(nintendosaves.read_title_list(self.tmp / "cemu" / "title_list_cache.xml")["00050000101B0400"]["name"], "Star Fox Zero")

    def test_detection_finds_the_flatpak_folders(self) -> None:
        home = self.tmp / "home"
        data = home / ".var" / "app" / "org.DolphinEmu.dolphin-emu" / "data" / "dolphin-emu"
        (data / "Wii").mkdir(parents=True)
        cfg = home / ".var" / "app" / "org.DolphinEmu.dolphin-emu" / "config" / "dolphin-emu"
        cfg.mkdir(parents=True)
        (cfg / "Dolphin.ini").write_text("[General]\nISOPaths = 1\nISOPath0 = /games/wii\n")
        got = nintendosaves.detect_dolphin(home)
        self.assertEqual((got["data"], got["games"]), (str(data), ["/games/wii"]))
        cemu = home / ".var" / "app" / "info.cemu.Cemu"
        (cemu / "data" / "Cemu" / "mlc01").mkdir(parents=True)
        (cemu / "config" / "Cemu").mkdir(parents=True)
        (cemu / "config" / "Cemu" / "settings.xml").write_text("<content><GamePaths><Entry>/games/wiiu</Entry></GamePaths><MRU><Entry>/x/a.wux</Entry></MRU></content>")
        got = nintendosaves.detect_cemu(home)
        self.assertEqual((got["mlc"], got["games"]), (str(cemu / "data" / "Cemu" / "mlc01"), ["/games/wiiu"]))
        self.assertEqual(nintendosaves.detect_dolphin(self.tmp / "nobody"), {})


def tdb(path: Path) -> None:
    (path.parent).mkdir(parents=True, exist_ok=True)
    wii = path.parent / "wii.txt.gz"
    wiiu = path.parent / "wiiu.txt.gz"
    wii.write_bytes(gzip.compress(b"TITLES = x\nSB4E01 = Super Mario Galaxy 2\nGMSE01 = Super Mario Sunshine\nRMGE01 = Super Mario Galaxy\n"))
    wiiu.write_bytes(gzip.compress(b"TITLES = x\nAFXE01 = Star Fox Zero\n"))
    gametdb.build({"wii": wii, "wiiu": wiiu}, path)


class Ps2(Base):
    def test_the_serial_of_an_iso_and_of_a_chd_less_text(self) -> None:
        from romorg import pcsx2
        self.assertEqual(pcsx2.read_serial(self.put("a.iso", ps2_iso("SLUS_209.46"))), "SLUS-20946")
        self.assertEqual(pcsx2.read_serial(self.put("b.iso", ps2_iso("SLES_523.60"))), "SLES-52360")
        self.assertEqual(pcsx2.read_serial(self.put("c.iso", b"x" * 100000)), "")
        self.assertEqual(pcsx2.serial_from_cnf("BOOT2 = cdrom0:\\SCUS_973.99;1\nVER = 1.0"), "SCUS-97399")
        self.assertEqual(pcsx2.serial_from_cnf("BOOT = cdrom0:\\SLPS_123.45;1"), "")                                   # (a PS1 disc: BOOT)

    def test_save_folder_names(self) -> None:
        from romorg import pcsx2
        self.assertEqual([pcsx2.save_serial(n) for n in ("BASLUS-20946GTA50000", "BESLES-54674c28_1", "BISLPS-25000X", "BADATA-SYSTEM", "_pcsx2_deleted_x")],
                         ["SLUS-20946", "SLES-54674", "SLPS-25000", "", ""])

    def test_a_card_image_is_read_through_its_file_system(self) -> None:
        from romorg import pcsx2
        card = ps2_card_image({"BASLUS-20946GTA50000": {"GTASAsf1.b": b"a" * 3000, "sub": {"x": b"b" * 10}}, "BADATA-SYSTEM": {"icon.sys": b"i" * 500}})
        for ecc in (False, True):
            path = self.put(f"Mcd{int(ecc)}.ps2", ps2_card_image({"BASLUS-20946GTA50000": {"GTASAsf1.b": b"a" * 3000, "sub": {"x": b"b" * 10}},
                                                                  "BADATA-SYSTEM": {"icon.sys": b"i" * 500}}, ecc=ecc))
            got = {s.name: (s.serial, s.files, s.bytes) for s in pcsx2.read_card_image(path)}
            self.assertEqual(got, {"BASLUS-20946GTA50000": ("SLUS-20946", 2, 3010), "BADATA-SYSTEM": ("", 1, 500)}, ecc)
        broken = pcsx2.read_card_image(self.put("bad.ps2", b"nothing" * 100))
        self.assertEqual([(s.kind, s.name) for s in broken], [("card", "bad.ps2")])
        del card

    def test_game_index(self) -> None:
        from romorg import pcsx2
        text = 'SLUS-20946:\n  name: "Grand Theft Auto - San Andreas"\n  region: "NTSC-U"\nSCUS-97399:\n  region: "NTSC-U"\n  name: "God of War"\n'
        self.assertEqual(pcsx2.parse_game_index(text), {"SLUS-20946": "Grand Theft Auto - San Andreas", "SCUS-97399": "God of War"})


def ps2_card_image(tree: dict, ecc: bool = False) -> bytes:
    """A PS2 memory card image holding ``tree`` (name -> bytes | dict): superblock, one FAT cluster, the root directory and the data."""
    page, per_cluster, clusters = 512, 2, 8192
    cluster_bytes = page * per_cluster
    stride = page + (16 if ecc else 0)
    store = {}                                   # cluster number -> 1024 bytes
    alloc_offset = 41
    nxt = [0]
    fat: dict = {}

    def alloc(data: bytes) -> int:
        """Put ``data`` in a chain of clusters; returns the first (allocatable) cluster index."""
        count = max(1, (len(data) + cluster_bytes - 1) // cluster_bytes)
        idx = list(range(nxt[0], nxt[0] + count))
        nxt[0] += count
        for i, c in enumerate(idx):
            store[alloc_offset + c] = data[i * cluster_bytes:(i + 1) * cluster_bytes].ljust(cluster_bytes, b"\0")
            fat[c] = 0x80000000 | idx[i + 1] if i + 1 < count else 0xFFFFFFFF
        return idx[0]

    def entry(mode: int, length: int, cluster: int, name: str) -> bytes:
        e = bytearray(512)
        struct.pack_into("<HHI", e, 0, mode, 0, length)
        e[8:16] = bytes([0, 1, 2, 3, 4, 5]) + struct.pack("<H", 2024)
        struct.pack_into("<I", e, 0x10, cluster)
        e[0x18:0x20] = bytes([0, 10, 20, 12, 15, 6]) + struct.pack("<H", 2024)
        e[0x40:0x40 + len(name)] = name.encode()
        return bytes(e)

    def directory(items: dict, parent_cluster: int) -> tuple:
        rows = []
        for name, value in items.items():
            if isinstance(value, dict):
                c, n = directory(value, 0)
                rows.append(entry(0x8427, n, c, name))
            else:
                rows.append(entry(0x8497, len(value), alloc(value) if value else 0, name))
        entries = [entry(0x8427, len(items) + 2, 0, "."), entry(0xA427, 0, 0, "..")] + rows
        return alloc(b"".join(entries)), len(entries)

    # the root directory is allocated first: cluster 0 of the allocatable area
    root_items = tree
    first, count = directory(root_items, 0)
    # the root's first entry must say how many entries there are (so entry 0 is "." with that length) - already so
    # FAT: cluster 1 holds the FAT entries, cluster 2 the indirect FAT
    fat_cluster = 1
    ifc_cluster = 2
    fat_bytes = bytearray(cluster_bytes)
    for c, v in fat.items():
        struct.pack_into("<I", fat_bytes, 4 * c, v)
    store[fat_cluster] = bytes(fat_bytes)
    ifc = bytearray(cluster_bytes)
    struct.pack_into("<I", ifc, 0, fat_cluster)
    store[ifc_cluster] = bytes(ifc)
    sb = bytearray(cluster_bytes)
    sb[:28] = b"Sony PS2 Memory Card Format "
    sb[28:40] = b"1.2.0.0".ljust(12, b"\0")
    struct.pack_into("<HHHH", sb, 0x28, page, per_cluster, 16, 0xFF00)
    struct.pack_into("<III", sb, 0x30, clusters, alloc_offset, 8135)
    struct.pack_into("<I", sb, 0x3C, first)
    struct.pack_into("<I", sb, 0x50, ifc_cluster)
    store[0] = bytes(sb)
    out = bytearray()
    for c in range(clusters):
        data = store.get(c, bytes(cluster_bytes))
        for p in range(per_cluster):
            out += data[p * page:(p + 1) * page] + (bytes(16) if ecc else b"")
    return bytes(out)


class Names(Base):
    def test_gametdb(self) -> None:
        tdb(self.tmp / "db" / "g.sqlite")
        db = gametdb.GameTdb(self.tmp / "db" / "g.sqlite")
        self.assertEqual((db.name("wii", "SB4E01"), db.name("gc", "GMSE01"), db.name("wiiu", "AFXE"), db.name("wii", "ZZZZ99")),
                         ("Super Mario Galaxy 2", "Super Mario Sunshine", "Star Fox Zero", ""))
        self.assertEqual(db.info()["titles"], 4)
        self.assertEqual(gametdb.check_update(path=self.tmp / "db" / "g.sqlite"), {"status": "up_to_date", "skipped": True})
        self.assertEqual(gametdb.check_update(path=self.tmp / "db" / "g.sqlite", now=__import__("time").time() + 5 * 86400)["status"], "update_available")
        self.assertEqual(gametdb.check_update(path=self.tmp / "none.sqlite"), {"status": "missing"})
        self.assertFalse(gametdb.GameTdb(self.tmp / "none.sqlite").available)


from tests.test_collection_api import CollectionCase  # noqa: E402  (sets ROMORG_RETROARCH_DETECT=0 first)


class Api(CollectionCase):
    ARCHIVE = False

    def setUp(self) -> None:
        super().setUp()
        self.home = self.tmp / "home"                                 # (nothing is installed: what this machine has must not leak in)
        self.home.mkdir()
        env = mock.patch.dict(os.environ, {"HOME": str(self.home), "APPDATA": "", "USERPROFILE": ""})
        env.start()
        self.addCleanup(env.stop)

    def test_detected_folders_are_used_until_the_user_chooses_otherwise(self) -> None:
        cemu = self.home / ".var" / "app" / "info.cemu.Cemu"
        (cemu / "data" / "Cemu" / "mlc01").mkdir(parents=True)
        (cemu / "config" / "Cemu").mkdir(parents=True)
        (cemu / "config" / "Cemu" / "settings.xml").write_text("<content/>")

        def cemu_row() -> dict:
            return next(e for e in self.call("GET", "/api/emulators")["emulators"] if e["key"] == "cemu")

        row = cemu_row()
        self.assertEqual((row["folder"], row["auto"], row["enabled"], row["active"]), (str(cemu / "data" / "Cemu"), True, True, True))
        mine = self.tmp / "mine"
        mine.mkdir()
        row = next(e for e in self.call("POST", "/api/emulators/config", {"source": "cemu", "folder": str(mine)})["emulators"] if e["key"] == "cemu")
        self.assertEqual((row["folder"], row["auto"]), (str(mine), False))
        self.call("POST", "/api/emulators/config", {"source": "cemu", "folder": str(cemu / "data" / "Cemu")})          # the found one again
        self.assertTrue(cemu_row()["auto"])
        self.call("POST", "/api/emulators/config", {"source": "cemu", "enabled": False})
        self.assertEqual((cemu_row()["enabled"], cemu_row()["active"]), (False, False))
        code, _ = self.http("POST", "/api/emulators/config", {"source": "wiiu"})
        self.assertEqual(code, 400)


if __name__ == "__main__":
    unittest.main()
