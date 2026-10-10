"""Nintendo Switch: games told by title ID from the container headers (no keys), the title database, the one optional key use
(the header of a CNMT NCA), and the saves of Eden and Ryujinx matched by title ID. Synthetic files only."""

from __future__ import annotations

import gzip
import json
import os
import struct
import tempfile
import unittest
from pathlib import Path

from romorg import switchapp, switchdb, switchfmt, switchkeys, switchsaves, switchscan

APP = "0100AAAA0BBB0000"
UPDATE = "0100AAAA0BBB0800"
DLC = "0100AAAA0BBB1001"


def pfs0(files: dict[str, bytes], magic: bytes = b"PFS0") -> bytes:
    """A PFS0 (or HFS0) with the given files."""
    names = b"".join(n.encode() + b"\0" for n in files)
    esz = 0x18 if magic == b"PFS0" else 0x40
    entries, off, name_off = b"", 0, 0
    for n, data in files.items():
        entry = struct.pack("<QQI", off, len(data), name_off)
        entries += entry + bytes(esz - len(entry))
        off += len(data)
        name_off += len(n.encode()) + 1
    return magic + struct.pack("<II", len(files), len(names)) + bytes(4) + entries + names + b"".join(files.values())


def cnmt_xml(title: str, kind: str, version: int) -> bytes:
    return (f'<?xml version="1.0"?><ContentMeta><Type>{kind}</Type><Id>0x{title.lower()}</Id><Version>{version}</Version>'
            f'</ContentMeta>').encode()


def xci(secure: dict[str, bytes]) -> bytes:
    """A game card image: 0x100 bytes, HEAD, the root HFS0 at 0x200 with one partition ("secure")."""
    part = pfs0(secure, b"HFS0")
    root_entries_len = 0x40
    names = b"secure\0"
    root_hdr = b"HFS0" + struct.pack("<II", 1, len(names)) + bytes(4)
    root_len = len(root_hdr) + root_entries_len + len(names)
    entry = struct.pack("<QQI", 0, len(part), 0) + bytes(root_entries_len - 20)
    root = root_hdr + entry + names + part
    head = bytearray(0x200)
    head[0x100:0x104] = b"HEAD"
    struct.pack_into("<QQ", head, 0x130, 0x200, root_len)
    return bytes(head) + root


def xts_encrypt_sector(key: bytes, data: bytes, sector: int) -> bytes:
    """The test's own encryption, the exact reverse of ``xts_decrypt_sector``."""
    k1, k2 = key[:16], key[16:]
    t = switchkeys.aes_encrypt_block(k2, sector.to_bytes(16, "big"))
    out = bytearray()
    for i in range(0, len(data), 16):
        block = bytes(a ^ b for a, b in zip(data[i:i + 16], t))
        enc = switchkeys.aes_encrypt_block(k1, block)
        out += bytes(a ^ b for a, b in zip(enc, t))
        t = switchkeys._times_alpha(t)
    return bytes(out)


def fake_nca(title: str, key: bytes) -> bytes:
    """An NCA of 0xC00 bytes whose header (sector 1) carries ``title``, encrypted with ``key``."""
    sector1 = bytearray(0x200)
    sector1[0:4] = b"NCA3"
    struct.pack_into("<Q", sector1, 0x10, int(title, 16))
    return bytes(0x200) + xts_encrypt_sector(key, bytes(sector1), 1) + bytes(0x800)


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="romorg-switch-")).resolve()
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)

    def put(self, rel: str, data: bytes) -> Path:
        p = self.tmp / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return p


class Ids(unittest.TestCase):
    def test_kinds_and_the_application_of_an_update_or_add_on(self) -> None:
        self.assertEqual([switchfmt.kind_of_id(x) for x in (APP, UPDATE, DLC)], ["application", "update", "addon"])
        self.assertEqual({switchfmt.base_id(x) for x in (APP, UPDATE, DLC)}, {APP})

    def test_the_tags_of_a_dump_name(self) -> None:
        self.assertEqual(switchfmt.tags_from_name("Zelda [0100F2C0115B6000][v655360][x].nsp"), ("0100F2C0115B6000", 655360))
        self.assertEqual(switchfmt.tags_from_name("Zelda v1.4.1[0100F2C0115B6800][589824][Site.com].nsp"), ("0100F2C0115B6800", 589824))
        self.assertEqual(switchfmt.tags_from_name("sxs-super_mario_bros_wonder_v65536.nsp"), (None, None))


class Containers(Base):
    def test_an_nsp_is_told_by_its_cnmt_xml_and_tickets(self) -> None:
        p = self.put("g/Some Game.nsp", pfs0({f"{UPDATE}{'0' * 15}4.tik": b"t", "abcd" * 8 + ".cnmt.xml": cnmt_xml(UPDATE, "Patch", 65536),
                                               "1" * 32 + ".nca": b"nca"}))
        c = switchfmt.read_container(p)
        self.assertEqual((c.kind, c.rights_ids, c.ticket_title_ids), ("nsp", [f"{UPDATE}{'0' * 15}4"], [UPDATE]))
        self.assertEqual((c.cnmt[0]["id"], c.cnmt[0]["version"], c.cnmt[0]["type"]), (UPDATE, 65536, "Patch"))
        self.assertEqual(c.nca_ids, ["1" * 32])

    def test_an_xci_lists_the_secure_partition(self) -> None:
        p = self.put("g/Card.xci", xci({"aa" * 16 + ".nca": b"x", "bb" * 16 + ".cnmt.nca": b"y" * 8}))
        c = switchfmt.read_container(p)
        self.assertEqual(c.kind, "xci")
        self.assertEqual([n for n, _s in c.files], ["aa" * 16 + ".nca", "bb" * 16 + ".cnmt.nca"])
        self.assertEqual(len(c.cnmt_nca), 1)
        with open(p, "rb") as f:                                   # (the offset really points at the file)
            f.seek(c.cnmt_nca[0][1])
            self.assertEqual(f.read(c.cnmt_nca[0][2]), b"y" * 8)

    def test_a_file_that_is_no_container_is_refused(self) -> None:
        with self.assertRaises(switchfmt.SwitchFormatError):
            switchfmt.read_container(self.put("x.nsp", b"nothing here at all"))


class Keys(Base):
    def test_aes_matches_fips_197_and_xts_round_trips(self) -> None:
        key = bytes(range(16))
        plain = bytes.fromhex("00112233445566778899aabbccddeeff")
        self.assertEqual(switchkeys.aes_encrypt_block(key, plain).hex(), "69c4e0d86a7b0430d8cdb78070b4c55a")
        self.assertEqual(switchkeys.aes_decrypt_block(key, switchkeys.aes_encrypt_block(key, plain)), plain)
        xk = bytes(range(32))
        data = os.urandom(0x200)
        self.assertEqual(switchkeys.xts_decrypt_sector(xk, xts_encrypt_sector(xk, data, 1), 1), data)

    def test_the_title_id_of_an_nca_and_a_wrong_key_gives_nothing(self) -> None:
        key = bytes(range(32))
        p = self.put("n.nca", fake_nca(APP, key))
        with open(p, "rb") as f:
            self.assertEqual(switchkeys.nca_title_id(f, 0, key), APP)
            self.assertIsNone(switchkeys.nca_title_id(f, 0, bytes(32)))

    def test_prod_keys_are_read_and_found(self) -> None:
        keys = self.put("keys/prod.keys", b"master_key_00 = 00\nheader_key = " + bytes(range(32)).hex().encode() + b"\n")
        self.assertEqual(switchkeys.load_keys(keys)["header_key"], bytes(range(32)))
        self.assertEqual(switchkeys.find_prod_keys([keys.parent]), keys)


def small_db(path: Path) -> switchdb.SwitchDb:
    titles = {"1": {"id": APP, "name": "Some Game", "publisher": "Pub", "releaseDate": 20240101},
              "2": {"id": None, "name": "No id"}}
    cnmts = {APP: {"0": {"titleId": APP, "titleType": 128, "contentEntries": [{"ncaId": "ab" * 16, "type": 1}], "otherApplicationId": UPDATE}},
             UPDATE: {"65536": {"titleId": UPDATE, "titleType": 129, "contentEntries": [{"ncaId": "cd" * 16, "type": 1}], "otherApplicationId": APP}}}
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path.with_name("t.json.gz"), "wt") as f:
        json.dump(titles, f)
    path.with_name("c.json").write_text(json.dumps(cnmts))
    switchdb.build(path.with_name("t.json.gz"), path.with_name("c.json"), path)
    return switchdb.SwitchDb(path)


class DatabaseUpdateCheck(Base):
    def test_age_gate_etags_and_missing(self) -> None:
        path = self.tmp / "db" / "titles.sqlite"
        self.assertEqual(switchdb.check_update(path=path), {"status": "missing"})
        small_db(path)
        self.assertEqual(switchdb.check_update(path=path), {"status": "up_to_date", "skipped": True})        # just built: no request
        heads = {switchdb.TITLES_URL: "t1", switchdb.CNMTS_URL: "c1", switchdb.BASE_URL + "GB.en.json": "g1", switchdb.BASE_URL + "JP.ja.json": "j1"}
        old = __import__("time").time() + 10 * 86400
        with mock.patch.object(switchdb, "_head_etag", lambda url, timeout: heads[url]):
            self.assertEqual(switchdb.check_update(path=path, now=old)["status"], "update_available")        # (no etags stored yet)
            conn = __import__("sqlite3").connect(path)
            conn.executemany("INSERT OR REPLACE INTO info VALUES (?,?)", [("etag_titles", "t1"), ("etag_cnmts", "c1"), ("etag_store_Europe", "g1"), ("etag_store_Japan", "j1")])
            conn.commit()
            conn.close()
            self.assertEqual(switchdb.check_update(path=path, now=old)["status"], "up_to_date")
            heads[switchdb.CNMTS_URL] = "c2"
            self.assertEqual(switchdb.check_update(path=path, now=old)["status"], "update_available")
        with mock.patch.object(switchdb, "_head_etag", side_effect=OSError("no route")):
            self.assertEqual(switchdb.check_update(path=path, now=old)["status"], "error")


class Database(Base):
    def test_build_and_lookups(self) -> None:
        db = small_db(self.tmp / "db" / "titles.sqlite")
        self.assertTrue(db.available)
        self.assertEqual((db.info()["titles"], db.info()["ncas"]), (1, 2))
        self.assertEqual(db.title(APP)["name"], "Some Game")
        self.assertEqual(db.nca("AB" * 16), (APP, 0, 1))
        self.assertEqual(db.application_of(UPDATE), APP)

    def test_a_missing_database_is_empty_not_an_error(self) -> None:
        db = switchdb.SwitchDb(self.tmp / "none.sqlite")
        self.assertFalse(db.available)
        self.assertIsNone(db.nca("ab" * 16))
        self.assertIsNone(db.title(APP))


class Scanning(Base):
    def test_every_way_of_telling_a_file(self) -> None:
        db = small_db(self.tmp / "db" / "titles.sqlite")
        key = bytes(range(32))
        self.put("games/a/game.nsp", pfs0({"x" * 32 + ".cnmt.xml": cnmt_xml(APP, "Application", 0), "1" * 32 + ".nca": b"."}))     # cnmt.xml
        self.put("games/b/renamed.nsp", pfs0({"cd" * 16 + ".nca": b"."}))                                                              # a known NCA
        self.put("games/c/ticket.nsp", pfs0({f"{DLC}{'0' * 16}.tik": b"t"}))                                                           # a ticket
        self.put("games/d/Card [0100BBBB0CCC0000][v0].xci", xci({"ee" * 16 + ".nca": b"."}))                                           # the name
        self.put("games/e/anonymous.xci", xci({"ff" * 16 + ".cnmt.nca": fake_nca("0100CCCC0DDD0000", key)}))                          # prod.keys
        self.put("games/f/mystery.nsp", pfs0({"11" * 16 + ".nca": b"."}))                                                              # nothing
        self.put("games/g/broken.nsp", b"not a container")
        self.put("games/h/readme.txt", b"ignored")
        told = {p.name: switchscan.identify(p, db, key) for p in sorted((self.tmp / "games").rglob("*.*")) if p.suffix in (".nsp", ".xci")}
        got = {name: (f.title_id, f.kind, f.version, f.how) for name, f in told.items() if f.title_id}
        self.assertEqual(got["game.nsp"], (APP, "application", 0, "cnmt.xml in the file"))
        self.assertEqual(got["renamed.nsp"], (UPDATE, "update", 65536, "title database (NCA ids)"))
        self.assertEqual(got["ticket.nsp"][:2], (DLC, "addon"))
        self.assertEqual(got["Card [0100BBBB0CCC0000][v0].xci"], ("0100BBBB0CCC0000", "application", 0, "file name"))
        self.assertEqual(got["anonymous.xci"][::3], ("0100CCCC0DDD0000", "NCA header (prod.keys)"))
        self.assertEqual(sorted(name for name, f in told.items() if not f.title_id), ["broken.nsp", "mystery.nsp"])
        self.assertEqual(told["Card [0100BBBB0CCC0000][v0].xci"].name, "Card")                   # (no database name: from the file name)
        self.assertEqual(told["game.nsp"].name, "Some Game")                                      # (the database's)
        self.assertEqual({told[n].base_id for n in ("game.nsp", "renamed.nsp", "ticket.nsp")}, {APP})    # one game, three files

    def test_a_wrong_name_is_caught_when_the_keys_can_tell(self) -> None:
        key = bytes(range(32))
        self.put("g/Wrong [0100BBBB0CCC0000].xci", xci({"ff" * 16 + ".cnmt.nca": fake_nca("0100CCCC0DDD0000", key)}))
        f = switchscan.identify(self.tmp / "g" / "Wrong [0100BBBB0CCC0000].xci", None, key)
        self.assertEqual(f.title_id, "0100CCCC0DDD0000")
        self.assertIn("0100BBBB0CCC0000", f.note)


def eden_save(root: Path, user: str, title: str, files: dict[str, bytes]) -> None:
    for rel, data in files.items():
        p = root / "user" / "save" / "0000000000000000" / user / title / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)


def ryu_save(root: Path, save_id: str, title: str, user_hi: int, kind: int, files: dict[str, bytes]) -> None:
    folder = root / "bis" / "user" / "save" / save_id
    (folder / "0").mkdir(parents=True, exist_ok=True)
    extra = bytearray(512)
    struct.pack_into("<QQQ", extra, 0, int(title, 16), user_hi, 0)
    extra[0x20] = kind
    (folder / "ExtraData0").write_bytes(bytes(extra))
    for rel, data in files.items():
        (folder / "0" / rel).write_bytes(data)


class Saves(Base):
    USER = "A188682A93BCBF166E4D6C9768953D1D"

    def test_eden_and_ryujinx_saves_are_found_and_compared_by_title_id(self) -> None:
        eden, ryu = self.tmp / "nand", self.tmp / "portable"
        eden_save(eden, self.USER, APP, {"a.sav": b"1234"})
        eden_save(eden, self.USER, "0100EEEE0FFF0000", {"x.sav": b"1"})
        eden_save(eden, "0" * 32, "0100EEEE0FFF0000", {"dev.sav": b"d"})                  # device data
        ryu_save(ryu, "0000000000000001", APP, 1, 1, {"a.sav": b"1234"})
        ryu_save(ryu, "0000000000000002", "0100GGGG1HHH0000".replace("G", "A").replace("H", "B"), 1, 1, {"z.sav": b"zz"})
        ryu_save(ryu, "0000000000000003", APP, 0, 3, {})                                   # device save of the same game, empty
        (ryu / "system").mkdir()
        (ryu / "system" / "Profiles.json").write_text(json.dumps({"profiles": [{"user_id": "00000000000000010000000000000000", "name": "Dom"}]}))
        e, r = switchsaves.find_eden(eden), switchsaves.find_ryujinx(ryu)
        self.assertEqual(sorted((s.title_id, s.kind) for s in e), [(APP, "account"), ("0100EEEE0FFF0000", "account"), ("0100EEEE0FFF0000", "device")])
        self.assertEqual(sorted((s.title_id, s.kind) for s in r), [(APP, "account"), (APP, "device"), ("0100AAAA1BBB0000", "account")])
        self.assertEqual(r[0].user, "00000000000000010000000000000000")

import hashlib

from romorg import switchverify


def nca_named(data: bytes) -> tuple[str, bytes]:
    """An NCA file as the archive names it: by the first 16 bytes of its SHA-256."""
    return hashlib.sha256(data).hexdigest()[:32] + ".nca", data


def game_nsp(title: str, version: int, payload: bytes = b"data") -> bytes:
    name, body = nca_named(payload + title.encode() + str(version).encode())
    return pfs0({f"{title}{'0' * 16}.tik": b"t", "m" * 32 + ".cnmt.xml": cnmt_xml(title, "Application" if title.endswith("000") else "Patch", version), name: body})


class Checksums(Base):
    def test_every_nca_is_checked_against_its_own_name(self) -> None:
        good = self.put("g/good.nsp", game_nsp(APP, 0))
        v = switchverify.verify_file(good)
        self.assertEqual((v.status, v.checked, v.bad), ("ok", 1, []))
        name, body = nca_named(b"original")
        bad = self.put("g/bad.nsp", pfs0({name: body[:-1] + b"X"}))                              # one byte changed
        v = switchverify.verify_file(bad)
        self.assertEqual((v.status, v.bad), ("damaged", [name]))

    def test_a_card_dump_is_checked_too_and_a_compressed_file_is_not(self) -> None:
        name, body = nca_named(b"card")
        self.assertEqual(switchverify.verify_file(self.put("g/card.xci", xci({name: body}))).status, "ok")
        z = self.put("g/zip.nsz", pfs0({"ab" * 16 + ".ncz": b"packed"}))
        v = switchverify.verify_file(z)
        self.assertEqual(v.status, "not checked")
        self.assertIn("compressed", v.why)

    def test_cancel_stops_and_the_verdict_is_remembered_for_the_same_file(self) -> None:
        p = self.put("g/good.nsp", game_nsp(APP, 0))
        with self.assertRaises(InterruptedError):
            switchverify.verify_file(p, None, None, lambda: True)
        cache = switchverify.VerifyCache(self.tmp / "v.sqlite")
        self.assertIsNone(cache.get(p))
        cache.put(p, switchverify.Verdict("ok", 1))
        self.assertEqual(cache.get(p).status, "ok")
        p.write_bytes(p.read_bytes() + b"x")                                                       # a different file now
        self.assertIsNone(cache.get(p))
        cache.close()


class Detection(Base):
    def test_a_portable_ryujinx_next_to_the_games_beats_the_standard_place(self) -> None:
        portable = self.tmp / "portable"
        (portable / "bis" / "user" / "save").mkdir(parents=True)
        (portable / "Config.json").write_text(json.dumps({"game_dirs": [str(portable / "games")]}))
        (portable / "games").mkdir()
        home = self.tmp / "home"
        (home / ".config" / "Ryujinx" / "bis").mkdir(parents=True)                      # (a standard install exists too)
        got = switchsaves.detect_ryujinx(home, near=[portable / "games"])
        self.assertEqual((got["data"], got["portable"]), (str(portable), True))
        std = switchsaves.detect_ryujinx(home)
        self.assertEqual((std["data"], std["portable"]), (str(home / ".config" / "Ryujinx"), False))
        self.assertEqual(switchsaves.detect_ryujinx(self.tmp / "nowhere"), {})


from unittest import mock

from tests.servercase import ServerCase  # noqa: E402  (sets ROMORG_RETROARCH_DETECT=0 first)


class Api(ServerCase):
    """The Switch page's endpoints over HTTP."""

    def setUp(self) -> None:
        super().setUp()
        self.home = self.tmp / "home"                                 # (nothing is installed: what this machine has must not leak in)
        self.home.mkdir()
        env = mock.patch.dict(os.environ, {"HOME": str(self.home), "APPDATA": "", "USERPROFILE": ""})
        env.start()
        self.addCleanup(env.stop)
        self.eden, self.ryu = self.tmp / "nand", self.tmp / "portable"
        eden_save(self.eden, Saves.USER, APP, {"a.sav": b"1234"})
        ryu_save(self.ryu, "0000000000000001", APP, 1, 1, {"a.sav": b"1234"})
        (self.tmp / "games").mkdir()
        (self.tmp / "games" / "Some Game [0100AAAA0BBB0000][v0].nsp").write_bytes(pfs0({f"{APP}{'0' * 16}.tik": b"t"}))
        self.call("POST", "/api/switch/config", {"games": str(self.tmp / "games"), "eden": str(self.eden), "ryujinx": str(self.ryu)})

    def test_settings_are_remembered_and_a_file_is_not_a_folder(self) -> None:
        info = self.call("GET", "/api/switch")
        self.assertEqual(info["config"]["eden"], str(self.eden))
        self.assertFalse(info["db"]["available"])
        (self.tmp / "afile").write_bytes(b"x")
        code, _ = self.http("POST", "/api/switch/config", {"games": str(self.tmp / "afile")})
        self.assertEqual(code, 400)
        self.call("POST", "/api/switch/config", {"ryujinx": ""})
        self.assertEqual(self.call("GET", "/api/switch")["config"]["ryujinx"], "")

    def test_what_is_found_on_the_machine_is_used_until_the_user_chooses_otherwise(self) -> None:
        # Eden's own settings name its NAND and a games folder; a portable Ryujinx lies next to that games folder
        portable = self.tmp / "emu" / "portable"
        (portable / "bis" / "user" / "save").mkdir(parents=True)
        (portable / "games").mkdir()
        nand = self.tmp / "emu" / "nand"
        (self.home / ".config" / "eden").mkdir(parents=True)
        (self.home / ".config" / "eden" / "qt-config.ini").write_text(
            f"nand_directory={nand}\nPaths\\gamedirs\\1\\path=SDMC\nPaths\\gamedirs\\2\\path={portable / 'games'}\n")
        self.call("POST", "/api/switch/config", {"games": "", "eden": "", "ryujinx": ""})
        info = self.call("GET", "/api/switch")
        self.assertEqual((info["config"]["games"], info["config"]["eden"], info["config"]["ryujinx"]),
                         (str(portable / "games"), str(nand), str(portable)))
        self.assertEqual(sorted(info["auto"]), ["eden", "games", "ryujinx"])
        # the user's own choice wins and is remembered; choosing what was found anyway keeps it automatic
        other = self.tmp / "elsewhere"
        other.mkdir()
        info = self.call("POST", "/api/switch/config", {"ryujinx": str(other), "eden": str(nand)})
        self.assertEqual((info["config"]["ryujinx"], info["auto"]), (str(other), ["eden", "games"]))
        self.assertEqual(self.call("GET", "/api/config" if False else "/api/switch")["config"]["eden"], str(nand))

if __name__ == "__main__":
    unittest.main()


class Stores(Base):
    """Where a title is sold (the store lists of titledb) gives its region, its languages and whether it is a demo."""

    def test_region_languages_and_demo_come_from_the_store_lists(self) -> None:
        def store(entries):
            return {str(i): e for i, e in enumerate(entries)}
        us = self.put("us.json", json.dumps(store([
            {"id": APP, "name": "Some Game™", "languages": ["en", "fr", "zh", "zh"], "isDemo": False},
            {"id": "0100CCCC0DDD0000", "name": "Free Demo", "languages": ["en"], "isDemo": True}])).encode())
        gb = self.put("gb.json", json.dumps(store([{"id": APP, "name": "Some Game", "languages": ["en", "de"]},
                                                   {"id": "0100EEEE0FFF0000", "name": "Europe Only", "languages": ["en"]}])).encode())
        jp = self.put("jp.json", json.dumps(store([{"id": APP, "name": "ゲーム", "languages": ["ja"]}])).encode())
        cn = self.put("cn.json", json.dumps({}).encode())
        out = self.tmp / "db" / "titles.sqlite"
        switchdb.build(us, cn, out, region_jsons={"Europe": gb, "Japan": jp})
        db = switchdb.SwitchDb(out)
        labels = {r[3] for r in db.catalogue()}
        self.assertEqual(labels, {"Some Game (World) (En,Fr,Zh,De,Ja)", "Free Demo (USA) (En) (Demo)", "Europe Only (Europe) (En)"})
        self.assertEqual(db.title("0100EEEE0FFF0000")["name"], "Europe Only")            # (a title only another store sells is known too)
        db.close()


class MoreDetection(Base):
    def test_eden_in_a_flatpak_and_on_windows_and_ryujinx_on_windows(self) -> None:
        home = self.tmp / "home"
        data = home / ".var" / "app" / "dev.eden_emu.eden" / "data" / "eden"
        (data / "nand").mkdir(parents=True)
        self.assertEqual(switchsaves.detect_eden(home)["nand"], str(data / "nand"))
        with mock.patch.dict(os.environ, {"APPDATA": str(self.tmp / "AppData")}):
            win = self.tmp / "AppData" / "eden"
            (win / "nand").mkdir(parents=True)
            (self.tmp / "AppData" / "Ryujinx" / "bis").mkdir(parents=True)
            empty = self.tmp / "empty"
            empty.mkdir()
            self.assertEqual(switchsaves.detect_eden(empty)["nand"], str(win / "nand"))
            self.assertEqual(switchsaves.detect_ryujinx(empty)["data"], str(self.tmp / "AppData" / "Ryujinx"))
