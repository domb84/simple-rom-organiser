"""Installing a WHDLoad game from ADFs without an Amiga: ``romorg.adf`` (AmigaDOS disks), ``romorg.amigainstall`` (the install
script run here, the .lha), and the rule "Prefer versions WHDLoad can install" (``romorg.whdready``)."""

from __future__ import annotations

import os
import shutil
import struct
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from romorg import amigainstall as ai, library, platforms, whdready
from romorg.adf import AdfDisk, AdfError
from romorg.datfile import Rom
from romorg.lha import LhaFile
from romorg.library import KEEP, Item

BLOCK = 512


def _hash(name: str) -> int:
    h = len(name)
    for c in name.upper():
        h = (h * 13 + ord(c)) & 0x7FF
    return h % 72


def make_adf(label: str, files: dict[str, bytes]) -> bytes:
    """An OFS floppy (``DOS\\0``) with ``files`` ("name" or "dir/name") - enough of AmigaDOS for the reader."""
    img = bytearray(901120)
    img[0:4] = b"DOS\x00"
    nxt = [882]

    def alloc() -> int:
        n = nxt[0]
        nxt[0] += 1
        return n

    def put(n: int, off: int, v: int) -> None:
        struct.pack_into(">I", img, n * BLOCK + off, v & 0xFFFFFFFF)

    def name(n: int, text: str) -> None:
        b = text.encode("latin-1")
        img[n * BLOCK + BLOCK - 80] = len(b)
        img[n * BLOCK + BLOCK - 79:n * BLOCK + BLOCK - 79 + len(b)] = b

    def link(parent: int, n: int, nm: str) -> None:
        slot = parent * BLOCK + 24 + 4 * _hash(nm)
        old = struct.unpack_from(">I", img, slot)[0]
        put(n, BLOCK - 16, old)                       # (the chain of names that hash alike)
        struct.pack_into(">I", img, slot, n)

    root = 880
    put(root, 0, 2)
    put(root, 12, 72)
    put(root, BLOCK - 4, 1)
    name(root, label)
    dirs = {"": root}
    for path, data in files.items():
        parts = path.split("/")
        parent = root
        for i, d in enumerate(parts[:-1]):
            key = "/".join(parts[:i + 1])
            if key not in dirs:
                b = alloc()
                put(b, 0, 2)
                put(b, 4, b)
                put(b, BLOCK - 4, 2)
                name(b, d)
                link(parent, b, d)
                dirs[key] = b
            parent = dirs[key]
        h = alloc()
        put(h, 0, 2)
        put(h, 4, h)
        put(h, BLOCK - 4, (1 << 32) - 3)
        put(h, BLOCK - 188, len(data))
        name(h, parts[-1])
        chunks = [data[i:i + 488] for i in range(0, len(data), 488)] or [b""]
        blocks = [alloc() for _ in chunks]
        put(h, 8, len(blocks))
        for i, (b, c) in enumerate(zip(blocks, chunks)):
            put(h, BLOCK - 204 - 4 * i, b)
            put(b, 0, 8)
            put(b, 4, h)
            put(b, 8, i + 1)
            put(b, 12, len(c))
            put(b, 16, blocks[i + 1] if i + 1 < len(blocks) else 0)
            img[b * BLOCK + 24:b * BLOCK + 24 + len(c)] = c
        link(parent, h, parts[-1])
    return bytes(img)


def make_icon(tooltypes: list[str], tool: str = "") -> bytes:
    """A project icon (a DiskObject) with a 16x1 image, ``tooltypes`` and an optional default tool, then trailing data."""
    head = bytearray(78)
    head[0:4] = b"\xe3\x10\x00\x01"
    struct.pack_into(">I", head, 22, 1)                                   # (an image)
    head[48] = 4                                                          # (a project)
    struct.pack_into(">I", head, 50, 1 if tool else 0)
    struct.pack_into(">I", head, 54, 1)
    image = struct.pack(">hhhhhI", 0, 0, 16, 1, 1, 1) + b"\x00" * 6 + b"\xff\xff"

    def text(t: str) -> bytes:
        b = t.encode() + b"\0"
        return struct.pack(">I", len(b)) + b
    body = (text(tool) if tool else b"") + struct.pack(">I", (len(tooltypes) + 1) * 4) + b"".join(text(t) for t in tooltypes)
    return bytes(head) + image + body + b"TRAILER"


class AdfTests(unittest.TestCase):
    def test_a_directory_chain_into_a_block_that_is_no_file(self) -> None:
        img = bytearray(make_adf("Game", {"game": b"exe"}))
        root = 880 * 512
        free = next(i for i in range(72) if not struct.unpack_from(">I", img, root + 24 + 4 * i)[0])
        struct.pack_into(">I", img, root + 24 + 4 * free, 2)          # (a hash slot to block 2: zeros, no file header)
        struct.pack_into(">I", img, root + 20, 0)
        struct.pack_into(">I", img, root + 20, (-sum(struct.unpack_from(">128I", img, root))) & 0xFFFFFFFF)
        d = AdfDisk(bytes(img))
        self.assertEqual([f for f, _s in d.files()], ["game"])         # (the others, as AmigaDOS lists them)
        self.assertEqual(d.damaged, {2})

    def test_files_of_an_ofs_disk(self) -> None:
        big = bytes(range(256)) * 9
        d = AdfDisk(make_adf("Game1", {"game": b"exe", "data/level1": big, "data/level1.info": b"i"}))
        self.assertEqual(d.label, "Game1")
        self.assertEqual(sorted(p for p, _s in d.files()), ["data/level1", "data/level1.info", "game"])
        self.assertEqual(d.read("DATA/Level1"), big)                     # (names compare without case, as on the Amiga)

    def test_a_disk_of_another_format_is_no_amigados_disk(self) -> None:
        with self.assertRaises(AdfError):
            AdfDisk(b"\x00" * 901120)


SCRIPT = """
(set #sub-dir "data")
(procedure P_AskDisk
  (askdisk (dest #AD_disk) (prompt "insert"))
)
(set #dest (askdir (prompt "Where?") (default @default-dest)))
(set #dest (tackon #dest @app-name))
(makedir #dest)
(copyfiles (source ("%s.slave" @app-name)) (dest #dest))
(set #dest (tackon #dest #sub-dir))
(makedir #dest)
(set #AD_disk "Game1")
(P_AskDisk)
(copyfiles (source ("%s:" #AD_disk)) (dest #dest) (pattern "~(#?.info)"))
(until (exists "DF0:level2") (message "Insert disk 2"))
(copyfiles (source "DF0:level2") (dest #dest))
(if (= (askbool (prompt "Run Game now?") (default 1)) 1) (run "WHDLoad Game.slave"))
(exit)
"""


class InstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.pkg = self.tmp / "GameInstall"
        self.pkg.mkdir()
        (self.pkg / "Game.slave").write_bytes(b"SLAVE")
        (self.pkg / "Install").write_text(SCRIPT)
        (self.pkg / "Install.info").write_bytes(b"\x00\x00APPNAME=Game\x00")
        self.disk1 = make_adf("Game1", {"game": b"exe", "game.info": b"icon", "level1": b"L1"})
        self.disk2 = make_adf("", {"level2": b"L2"})                      # (no label: found by its file)

    def test_the_script_installs_the_game_from_the_disks(self) -> None:
        res = ai.install(self.pkg, [self.disk1, self.disk2])
        self.assertEqual(sorted(res.files), ["Game/Game.slave", "Game/data/game", "Game/data/level1", "Game/data/level2", "load"])   # (no icon: load starts it)
        self.assertEqual(res.files["Game/data/level2"], b"L2")
        out = self.tmp / "Game.lha"
        ai.write_lha(out, res.files)
        a = LhaFile(out)
        self.assertEqual({e.name: a.read(e) for e in a.entries}, res.files)
        self.assertEqual({e.level for e in a.entries}, {1})                  # (as WHDLoad's own archives)

    def test_a_disk_that_is_not_there_stops_it(self) -> None:
        with self.assertRaisesRegex(ai.InstallError, "waits for a disk"):
            ai.install(self.pkg, [self.disk1])                            # (disk 2 is missing)

    def test_an_empty_drawer_the_install_makes_is_in_the_archive(self) -> None:
        out = self.tmp / "g.lha"
        ai.write_lha(out, {"Game/Game.slave": b"S"}, {"Game", "Game/save"})       # (a save drawer the game expects)
        self.assertEqual([(e.name, e.is_dir) for e in LhaFile(out).entries], [("Game/Game.slave", False), ("Game/save", True)])

    def test_aga_disks_get_the_aga_version(self) -> None:
        self.assertEqual(ai.chipset_hints(["Game (1994)(Pub)(AGA)(Disk 1 of 2).adf"]), ("AGA",))
        self.assertEqual(ai.chipset_hints(["Game (1994)(Pub)(OCS-AGA).adf"]), ())
        self.assertEqual(ai.chipset_hints(["Game (1994)(Pub)(ECS).adf"]), ("ECS", "OCS"))
        (self.pkg / "GameAGA.slave").write_bytes(b"AGA")
        (self.pkg / "Install").write_text(          # (a chipset question outside the version list: Theme Park's)
            '(set #v (askchoice (prompt "Which version of Game do you like to install?") (default 0) (choices "ECS" "AGA" "CD32")))'
            '(select #v (set #c "") (set #c "AGA") (set #c "CD32"))'
            '(copyfiles (source ("Game%s.slave" #c)) (dest "WORK:Game") (newname "Game.slave"))')
        self.assertEqual(ai.install(self.pkg, [], hints=("AGA",)).files["Game/Game.slave"], b"AGA")
        self.assertEqual(ai.install(self.pkg, [], hints=("ECS", "OCS")).files["Game/Game.slave"], b"SLAVE")
        script = ('(set #multiple-versions "yes")\n(procedure P_SelectVersion (set #version (askchoice (prompt "Which version?") '
                  '(default 0) (choices "OCS version" "AGA version"))))\n(P_SelectVersion)\n'
                  '(if (= #version 1) (copyfiles (source "GameAGA.slave") (dest "WORK:Game") (newname "Game.slave"))'
                  ' (copyfiles (source "Game.slave") (dest "WORK:Game")))\n')
        (self.pkg / "Install").write_text(script)
        ai.write_lha(self.tmp / "pkg.lha", {f"GameInstall/{p.name}": p.read_bytes() for p in self.pkg.iterdir()})
        with ai.Package((self.tmp / "pkg.lha").read_bytes()) as pkg:
            self.assertEqual(pkg.install([], hints=("AGA",)).files["Game/Game.slave"], b"AGA")
            self.assertEqual(pkg.install([]).files["Game/Game.slave"], b"SLAVE")              # (no hint: the default)

    def test_a_hidden_choice_is_never_an_answer(self) -> None:
        (self.pkg / "Images.slave").write_bytes(b"IMAGES")     # (Explora's install: "" for the RawDIC / Patcher / DIC slots it does not have)
        script = ('(set #multiple-versions ">")\n(procedure P_SelectVersion (set #version (askchoice (prompt "Which version?") '
                  '(default 0) (choices "" "" "Explora" "Chrono Quest"))))\n(P_SelectVersion)\n'
                  '(select #version (copyfiles (source "Images.slave") (dest "WORK:Game") (newname "Game.slave")) (abort "no")'
                  ' (askdisk (dest "Explora.Game.Disk")) (copyfiles (source "Game.slave") (dest "WORK:Game")))\n')
        (self.pkg / "Install").write_text(script)
        self.assertEqual(ai.install(self.pkg, []).files["Game/Game.slave"], b"SLAVE")         # (not the hidden first slot's images)

    def test_a_game_file_asked_for_is_found_on_the_disks(self) -> None:
        (self.pkg / "Install").write_text(          # (Treasure Island Dizzy: "select the game file (DIZZY.DAT, Tid ...)" on an unknown disk)
            '(set #f (askfile (prompt "Please select the game file (DIZZY.DAT, Tid or treasure)") (default "DIZZY.DAT")))'
            '(copyfiles (source "Game.slave") (dest "WORK:Game")) (copyfiles (source #f) (dest "WORK:Game/data"))')
        disk = make_adf("TRESURE_ISLAND_DIZZY", {"dizzy.dat": b"the game", "s/startup-sequence": b"dizzy"})
        self.assertEqual(ai.install(self.pkg, [disk]).files["Game/data/dizzy.dat"], b"the game")

    def test_the_shell_copy_command(self) -> None:
        (self.pkg / "Install").write_text(          # (Rockford: everything but .fastdir, l and s; Strider II: df0:#? into data)
            '(copyfiles (source "Game.slave") (dest "WORK:Game"))'
            '(run "Copy Rockford:~(.fastdir|l|s) TO \\"WORK:Game\\" ALL CLONE")'
            '(makedir "WORK:Game/data") (run "copy df0:rf to \\"WORK:Game/data\\" QUIET")')   # (into a drawer that is there)
        disk = make_adf("Rockford", {".fastdir": b"x", "rf": b"RF", "LEV/astr": b"A", "s/startup-sequence": b"rf", "l/Disk-Validator": b"v"})
        files = ai.install(self.pkg, [disk]).files
        self.assertEqual(sorted(k for k in files if k.startswith("Game/")),
                         ["Game/Game.slave", "Game/LEV/astr", "Game/data/rf", "Game/rf"])

    def test_every_option_the_installer_ticks_and_else_the_first(self) -> None:
        (self.pkg / "Install").write_text(          # (Test Drive II: the program disk and data disks no one has - the program disk then)
            '(set #o (askoptions (prompt "What do you want to install?") (choices "Program Disk" "Scenery Disk")))'
            '(copyfiles (source "Game.slave") (dest "WORK:Game"))'
            '(if (BITAND #o 1) (copyfiles (source "Game1:game") (dest "WORK:Game")))'
            '(if (BITAND #o 2) (askdisk (dest "SceneryDisk")))')
        self.assertIn("Game/game", ai.install(self.pkg, [self.disk1]).files)

    def test_a_slave_s_data_drawer_its_old_script_does_not_make(self) -> None:
        import struct
        slave = bytearray(b"\x70\xff\x4e\x75WHDLOADS" + bytes(40) + b"data\0")
        struct.pack_into(">H", slave, 4 + 26 - 4, len(slave) - 5)               # (ws_CurrentDir: "data", as Maniax's v17 slave)
        files = {"Maniax/maniax.slave": bytes(slave), "Maniax/Maniax.info": make_icon(["SLAVE=maniax.slave"], "WHDLoad"),
                 "Maniax/Readme": b"r", "Maniax/Readme.info": make_icon([], "MultiView"), "Maniax/Athens.pic": b"p", "Maniax/Maniax": b"exe",
                 "Maniax/C/run": b"c"}
        self.assertEqual(len(ai.repair_current_dir(files)), 1)
        self.assertEqual(sorted(files), ["Maniax/Maniax.info", "Maniax/Readme", "Maniax/Readme.info", "Maniax/data/Athens.pic",
                                         "Maniax/data/C/run", "Maniax/data/Maniax", "Maniax/maniax.slave"])
        self.assertEqual(ai.repair_current_dir(files), [])                      # (there now)
        files["Maniax/data/maniax.slave"] = bytes(slave)                         # (a stray copy of the slave in data: Magicland Dizzy's
        self.assertEqual(ai.repair_current_dir(files), [])                      #  old script copies the package there - never data/data)
        files = {"GodFather/GodFather.slave": bytes(slave), "GodFather/GodFatherData/GODFATHER": b"exe", "GodFather/GodFatherData/a.iff": b"a"}
        self.assertEqual(len(ai.repair_current_dir(files)), 1)                  # (all in the old slave's drawer: renamed, not data/GodFatherData)
        self.assertEqual(sorted(files), ["GodFather/GodFather.slave", "GodFather/data/GODFATHER", "GodFather/data/a.iff"])

    def test_a_slave_no_icon_starts_gets_a_load_script(self) -> None:
        self.assertEqual(ai.puae_launcher({"BombX/BombX.slave": b"S", "BombX/data/x": b"x"}), b'CD "BombX"\nC:WHDLoad "BombX.slave" Preload\n')
        self.assertIsNone(ai.puae_launcher({"BombX/BombX.slave": b"S", "BombX/BombX.info": b"I"}))

    def test_xadunfile_unpacks_a_zip(self) -> None:
        import io
        import zipfile
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("tube", b"the game")
        (self.pkg / "Install").write_text(          # (Tubular Worlds' AGA "code")
            '(copyfiles (source "Game.slave") (dest "WORK:Game")) (copyfiles (source "TW1:code") (dest "WORK:Game/data"))'
            '(run "xadUnFile WORK:Game/data/code WORK:Game/data")')
        self.assertEqual(ai.install(self.pkg, [make_adf("TW1", {"code": buf.getvalue()})]).files["Game/data/tube"], b"the game")

    def test_a_kickstart_the_emulator_gives_whdload(self) -> None:
        (self.pkg / "Install").write_text(          # (Subwar 2050's check: the slave boots the real Kickstart 3.1)
            '(if (exists "DEVS:Kickstarts/Kick40068.A1200") (set #k 1) (if (exists "DEVS:Kickstarts/Kick40068.A4000") (set #k 1)'
            ' (abort "No 3.1 kickstart file found in DEVS:Kickstarts, cannot continue")))'
            '(if (exists "DEVS:Kickstarts/kick99999.A9000") (abort "not one there is"))'
            '(copyfiles (source "Game.slave") (dest "WORK:Game"))')
        self.assertEqual(sorted(ai.install(self.pkg, []).files), ["Game/Game.slave", "load"])

    def test_an_icon_naming_a_slave_that_is_not_there(self) -> None:
        files = {"ShadowOfTheBeast3/ShadowOfTheBeast3.slave": b"S", "ShadowOfTheBeast3/ShadowOfTheBeast3.info": make_icon(["SLAVE=Beast3.slave", "PRELOAD"], "WHDLoad"),
                 "ShadowOfTheBeast3/ReadMe.info": make_icon(["FILETYPE=Text"])}
        log = ai.repair_slave_tooltypes(files)
        self.assertEqual(ai.icon_info(files["ShadowOfTheBeast3/ShadowOfTheBeast3.info"])[1], ["SLAVE=ShadowOfTheBeast3.slave", "PRELOAD"])
        self.assertEqual(len(log), 1)
        self.assertEqual(ai.repair_slave_tooltypes(files), [])                              # (right now: nothing to do)
        files = {"Doman/Doman.Slave": b"S", "Doman/Doman.info": make_icon([""], "WHDLoad"), "Doman/ReadMe.info": make_icon([], "MultiView")}
        self.assertEqual(len(ai.repair_slave_tooltypes(files)), 1)                          # (a WHDLoad icon with no Slave=)
        self.assertIn("Slave=Doman.Slave", ai.icon_info(files["Doman/Doman.info"])[1])
        self.assertEqual(ai.icon_info(files["Doman/ReadMe.info"])[1], [])

    def test_disk_images_in_a_data_drawer_the_icon_does_not_name(self) -> None:
        def slave(cur: bytes) -> bytes:                            # (a WHDLoad slave header: ws_CurrentDir at $1A, its string after)
            head = b"\x70\xff\x4e\x75WHDLOADS" + struct.pack(">HHIIHH", 16, 0, 0x80000, 0, 0, 0x20 if cur else 0)
            return head + bytes(0x20 - len(head)) + cur + b"\0"
        files = {"CSB/CSB.Slave": slave(b""), "CSB/CSB.info": make_icon(["PreLoad", "Slave=CSB.Slave"], "WHDLoad"),
                 "CSB/data/Disk.1": b"D1", "CSB/ReadMe.info": make_icon([], "MultiView")}
        self.assertEqual(ai.slave_current_dir(files["CSB/CSB.Slave"]), "")
        self.assertEqual(len(ai.repair_data_tooltype(files)), 1)                            # (Chaos Strikes Back's install)
        self.assertIn("Data=data", ai.icon_info(files["CSB/CSB.info"])[1])
        self.assertEqual(ai.repair_data_tooltype(files), [])                                # (done: nothing more)
        files = {"G/G.Slave": slave(b"data"), "G/G.info": make_icon(["Slave=G.Slave"], "WHDLoad"), "G/data/Disk.1": b"D1"}
        self.assertEqual(ai.slave_current_dir(files["G/G.Slave"]), "data")
        self.assertEqual(ai.repair_data_tooltype(files), [])                                # (the slave names its drawer itself)

    def test_a_comparison_evaluates_every_argument(self) -> None:
        (self.pkg / "Install").write_text('(if (= 0 (set #x 1) (copyfiles (source "Game.slave") (dest "WORK:Game"))) ("") ("")) ')
        self.assertIn("Game/Game.slave", ai.install(self.pkg, []).files)

    def test_a_drawer_with_several_slaves_gets_a_load_script(self) -> None:
        files = {"WormsDC/WormsDC.Slave": b"S", "WormsDC/WormsDC_8MB.Slave": b"S8", "WormsDC/WormsDC_12MB.Slave": b"S12",
                 "WormsDC/WormsDC 8MB.info": make_icon(["SLAVE=WormsDC_8MB.Slave"], "WHDLoad"),
                 "WormsDC/WormsDC.info": make_icon(["PRELOAD", "SLAVE=WormsDC.Slave"], "WHDLoad")}
        self.assertEqual(ai.puae_launcher(files), b'CD "WormsDC"\nC:WBRun "WormsDC" SYNC\n')     # (the game's own icon)
        files = {"Fuzz/FuzzballIntro.slave": b"I", "Fuzz/Fuzzball.slave": b"G"}                # (no icons: the plainest slave)
        self.assertEqual(ai.puae_launcher(files), b'CD "Fuzz"\nC:WHDLoad "Fuzzball.slave" Preload\n')
        self.assertIsNone(ai.puae_launcher({"Game/Game.slave": b"S", "Game/Game.info": b"I"}))   # (one slave: PUAE finds it)
        self.assertEqual(ai.puae_launcher({"Total Football/Total Football.Slave": b"S"}),         # (a space breaks PUAE's script)
                         b'CD "Total Football"\nC:WHDLoad "Total Football.Slave" Preload\n')
        (self.pkg / "Game8MB.slave").write_bytes(b"BIG")
        (self.pkg / "Install").write_text('(copyfiles (source "Game.slave") (dest "WORK:Game")) (copyfiles (source "Game8MB.slave") (dest "WORK:Game"))')
        self.assertEqual(ai.install(self.pkg, []).files["load"], b'CD "Game"\nC:WHDLoad "Game.slave" Preload\n')

    def test_a_package_with_no_script(self) -> None:
        import struct
        hdr = b"\x70\xff\x4e\x75WHDLOADS" + struct.pack(">HHIIHH", 17, 0, 0x80000, 0, 0, 40) + bytes(12) + b"data\0"
        files = {"Rubicon/Rubicon.slave": hdr, "Rubicon/Rubicon.inf": b"\xe3\x10icon", "Rubicon/ReadMe": b"use DIC",
                 "Rubicon/source/Rubicon.s": b"; the source"}
        ai.write_lha(self.tmp / "pkg.lha", files)
        disk = make_adf("Rubicon", {"game": b"exe", "levels/l1": b"L1"})
        with ai.Package((self.tmp / "pkg.lha").read_bytes()) as pkg:
            res = pkg.install([disk])
        self.assertEqual(res.files["Rubicon/Rubicon.slave"], hdr)
        self.assertEqual(res.files["Rubicon/Rubicon.info"], b"\xe3\x10icon")
        self.assertEqual((res.files["Rubicon/data/game"], res.files["Rubicon/data/levels/l1"]), (b"exe", b"L1"))
        self.assertEqual(res.files["Rubicon/data/Disk.1"], disk)                 # (a DIC image of a standard disk is the ADF)
        self.assertIn("Rubicon/ReadMe", res.files)
        self.assertNotIn("Rubicon/source/Rubicon.s", res.files)
        self.assertEqual(ai.slave_dir(b"no slave"), "")

    def test_the_script_named_install_is_the_one_run(self) -> None:
        (self.pkg / "Install.").write_text('(abort "an old script")')                 # (a stray copy: ConanTheCimmerian has one)
        (self.pkg / "Install").write_text('(copyfiles (source "Game.slave") (dest "WORK:Game"))')
        self.assertEqual(sorted(ai.install(self.pkg, []).files), ["Game/Game.slave", "load"])
        ai.write_lha(self.tmp / "pkg.lha", {f"GameInstall/{p.name}": p.read_bytes() for p in self.pkg.iterdir()})
        with ai.Package((self.tmp / "pkg.lha").read_bytes()) as pkg:
            self.assertEqual(sorted(pkg.install([]).files), ["Game/Game.slave", "load"])

    def test_without_a_plain_install_each_script_is_tried(self) -> None:
        (self.pkg / "Install").unlink()
        (self.pkg / "Install_AGA").write_text('(abort "needs the AGA disks")')
        (self.pkg / "Install_OCS").write_text('(copyfiles (source "Game.slave") (dest "WORK:Game"))')
        (self.pkg / "Installation.readme").write_text("how to install")
        ai.write_lha(self.tmp / "pkg.lha", {f"GameInstall/{p.name}": p.read_bytes() for p in self.pkg.iterdir()})
        with ai.Package((self.tmp / "pkg.lha").read_bytes()) as pkg:
            self.assertEqual([p.name for p in pkg.scripts], ["Install_AGA", "Install_OCS"])
            self.assertEqual(sorted(pkg.install([]).files), ["Game/Game.slave", "load"])

    def test_a_missing_normal_icon_falls_back_to_another_of_the_package(self) -> None:
        (self.pkg / "Game.GlowIcon").write_bytes(make_icon(["SLAVE=Game.slave"], "WHDLoad"))      # (no Game.inf: only the GlowIcon)
        (self.pkg / "Install").write_text('(set #dest "WORK:Game") (makedir #dest) (copyfiles (source "Game.slave") (dest #dest)) '
                                          '(copyfiles (source "Game.inf") (dest #dest) (newname "Game.info"))')
        res = ai.install(self.pkg, [])
        self.assertEqual(res.files["Game/Game.info"][:2], b"\xe3\x10")
        self.assertTrue(any("its icon Game.GlowIcon instead" in line for line in res.log))

    def test_dic_leaves_skipped_tracks_empty(self) -> None:
        (self.pkg / "Install").write_text('(set #dest "WORK:Game") (makedir #dest) (copyfiles (source "Game.slave") (dest #dest)) '
                                          '(run ("cd \\"%s\\"\\nDIC DF0: FD=1 LD=1 SIZE=$b2c00 SKIPTRACK=1" #dest))')
        img = ai.install(self.pkg, [self.disk1]).files["Game/Disk.1"]
        self.assertEqual(len(img), 0xb2c00)
        self.assertEqual(img[0x1600:0x2c00], bytes(0x1600))
        self.assertEqual(img[:4], b"DOS\x00")

    def test_dic_images_a_disk_that_is_no_amigados_disk(self) -> None:
        own = b"\x00" * 901120                                              # (a game's own format: no DOS boot block)
        (self.pkg / "Install").write_text('(set #dest "WORK:Game") (makedir #dest) (copyfiles (source "Game.slave") (dest #dest)) '
                                          '(run ("cd \\"%s\\"\\nDIC DF0: FD=1 LD=2" #dest))')
        res = ai.install(self.pkg, [self.disk1, own])
        self.assertEqual(res.files["Game/Disk.2"], own)
        (self.pkg / "Install").write_text('(set #dest "WORK:Game") (copyfiles (source "DF0:game") (dest #dest))')
        with self.assertRaisesRegex(ai.InstallError, "not an AmigaDOS disk"):                # (files from it: why not)
            ai.install(self.pkg, [own])

    def test_floppies_are_not_made_into_the_cd_version(self) -> None:
        script = ('(set #multiple-versions "yes")\n(procedure P_SelectVersion (set #version (askchoice (prompt "Which version?") '
                  '(default 0) (choices "Floppy release" "CD³² release"))))\n(P_SelectVersion)\n'
                  '(if (= #version 1) (copyfiles (source "Game.slave") (dest "WORK:Game")) (abort "RawDIC needed"))\n')
        (self.pkg / "Install").write_text(script, encoding="latin-1")                # (as the Amiga's scripts are)
        ai.write_lha(self.tmp / "pkg.lha", {f"GameInstall/{p.name}": p.read_bytes() for p in self.pkg.iterdir()})
        with ai.Package((self.tmp / "pkg.lha").read_bytes()) as pkg:
            with self.assertRaisesRegex(ai.InstallError, "RawDIC"):
                pkg.install([self.disk1])                                      # (floppies: not the CD version)
            self.assertEqual(pkg.install([self.disk1], cd=True).version, 1)    # (a CD's files: that one)

    def test_a_disk_a_cracker_renamed_is_found_by_its_number(self) -> None:
        renamed = make_adf("Crack Disk", {"game": b"exe", "level1": b"L1"})
        res = ai.install(self.pkg, [renamed, self.disk2])
        self.assertEqual(res.files["Game/data/level1"], b"L1")

    def test_a_disk_named_like_the_hard_disk_is_that_disk(self) -> None:
        (self.pkg / "Install").write_text('(set #dest (tackon @default-dest "Game")) (makedir #dest) (copyfiles (source "Game.slave") (dest #dest)) '
                                          '(askdisk (dest "Work")) (copyfiles (source "Work:") (dest #dest) (pattern "~(#?.info)"))')
        for label in ("Work", "Ninja"):                                    # (labelled "Work", or the only disk)
            res = ai.install(self.pkg, [make_adf(label, {"game": b"exe", "game.info": b"i"})])
            self.assertEqual(sorted(res.files), ["Game/Game.slave", "Game/game", "load"])

    def test_disks_that_share_a_label_are_told_apart_by_their_files(self) -> None:
        # (Fury of the Furries: disks 2-3 are both "FURY2", the script waits for the file that tells which; AB3D2: the 2 MB and 4 MB
        #  program disks are both "TKG1", the 4 MB version copies "hqn" too - and the drawers that differ in case are one)
        (self.pkg / "Install").write_text(
            '(set #dest "WORK:Game") (makedir #dest) (copyfiles (source "Game.slave") (dest #dest))'
            '(until (exists "G2:b/two") (message "insert disk 3")) (askdisk (dest "G2")) (copyfiles (source "G2:") (dest #dest) (pattern "~(#?.info)"))'
            '(askdisk (dest "P1")) (copyfiles (source "P1:") (dest #dest) (pattern "(hqn|includes)"))'
            '(askdisk (dest "L")) (copyfiles (source "L:") (dest #dest) (pattern "INCLUDES"))')
        disks = [make_adf("G2", {"a/one": b"1"}), make_adf("G2", {"B/two": b"2"}), make_adf("P1", {"includes/s": b"small"}),
                 make_adf("P1", {"hqn/h": b"H", "Includes/b": b"big"}), make_adf("L", {"INCLUDES/t": b"T"})]
        res = ai.install(self.pkg, disks)
        self.assertEqual(sorted(res.files), ["Game/B/two", "Game/Game.slave", "Game/Includes/b", "Game/Includes/t", "Game/hqn/h", "load"])

    def _ask(self, volumes: list[str], disks: list[bytes]) -> "ai.InstallResult":
        """An install that asks for ``volumes`` in turn and copies each disk's ``f`` to a file of its own."""
        body = "".join(f'(askdisk (dest "{v}")) (copyfiles (source "{v}:f") (dest #dest) (newname "from{n}"))' for n, v in enumerate(volumes, 1))
        (self.pkg / "Install").write_text('(set #dest "WORK:Game") (makedir #dest) (copyfiles (source "Game.slave") (dest #dest)) ' + body)
        return ai.install(self.pkg, disks)

    def test_disks_a_script_knows_by_another_name(self) -> None:
        d = [make_adf("x1", {"f": b"one"}), make_adf("x2", {"f": b"two"}), make_adf("x3", {"f": b"three"})]
        got = lambda r: [r.files[f"Game/from{n}"] for n in (1, 2, 3)]
        r = self._ask(["X 1", "x-2", "X3"], d)                                          # (the label, ignoring spaces and punctuation)
        self.assertEqual((got(r), r.guessed), ([b"one", b"two", b"three"], 0))
        r = self._ask(["ABRA III", "ABRA I", "ABRA II"], d)                             # (a Roman numeral is the disk's number)
        self.assertEqual((got(r), r.guessed), ([b"three", b"one", b"two"], 0))
        r = self._ask(["Alpha", "Beta", "Gamma"], d)                                    # (no label, no number: the order they are asked for)
        self.assertEqual((got(r), r.guessed), ([b"one", b"two", b"three"], 3))
        (self.pkg / "Install").write_text('(set #dest "WORK:Game") (makedir #dest) (copyfiles (source "Game.slave") (dest #dest)) '
                                          '(askdisk (dest "My Saved Games")) (askdisk (dest "Game Disk 2")) '
                                          '(copyfiles (source "Game Disk 2:f") (dest #dest) (newname "got"))')
        r = ai.install(self.pkg, d[:2])                                                  # (a disk for saved games is a blank one)
        self.assertEqual((r.files["Game/got"], r.guessed), (b"two", 0))

    def test_a_script_waiting_for_a_file_finds_the_disk_that_has_it(self) -> None:
        (self.pkg / "Install").write_text('(set #dest "WORK:Game") (makedir #dest) (copyfiles (source "Game.slave") (dest #dest)) '
                                          '(until (exists "PROGDISK:main") (message "Insert it")) (copyfiles (source "PROGDISK:main") (dest #dest))')
        res = ai.install(self.pkg, [make_adf("a", {"data": b"d"}), make_adf("b", {"main": b"M"})])
        self.assertEqual((res.files["Game/main"], res.guessed), (b"M", 0))               # (found by its content, not guessed)

    def test_dic_images_the_disks_in_their_order(self) -> None:
        (self.pkg / "Install").write_text('(set #dest "WORK:Game") (makedir #dest) (copyfiles (source "Game.slave") (dest #dest)) '
                                          '(run ("cd \\"%s\\"\\nDIC DF0: FD=1 LD=2 SIZE=901120" #dest))')
        res = ai.install(self.pkg, [self.disk1, self.disk2])
        self.assertEqual((res.files["Game/Disk.1"], res.files["Game/Disk.2"]), (self.disk1, self.disk2))

    def test_rawdic_cannot_be_done_here(self) -> None:
        (self.pkg / "Install").write_text('(set #dest "WORK:Game") (run ("CD \\"%s\\"\\nRawDIC SLAVE=Game.islave" #dest))')
        with self.assertRaisesRegex(ai.InstallError, "RawDIC"):
            ai.install(self.pkg, [self.disk1])

    def test_a_script_that_asks_for_the_version_is_tried_with_each(self) -> None:
        script = ('(set #multiple-versions "yes")\n(procedure P_SelectVersion (set #version (askchoice (prompt "Which version?") '
                  '(default 0) (choices "" "Disk version"))))\n(P_SelectVersion)\n'
                  '(if (= #version 1) ((askdisk (dest "Game1")) (copyfiles (source "Game.slave") (dest "WORK:Game"))) '
                  '(abort "no such version"))\n')
        (self.pkg / "Install").write_text(script)
        self.assertEqual(ai.version_choices(script), [1])
        self.assertEqual(ai.version_choices('(set #multiple-versions "<")\n(procedure P_SelectVersion (askchoice (choices "" ; x)\n'
                                            ' "" "ECS version (4 disks)" "AGA version (5 disks)")))'), [2, 3])
        ai.write_lha(self.tmp / "pkg.lha", {f"GameInstall/{p.name}": p.read_bytes() for p in self.pkg.iterdir()})
        res = ai.install_archive((self.tmp / "pkg.lha").read_bytes(), [self.disk1])
        self.assertEqual(res.version, 1)
        self.assertIn("Game/Game.slave", res.files)

    def test_the_icon_gets_the_slave_and_whdload(self) -> None:
        # (an install's icon template names another slave and has no default tool: WHDLoad would not start it)
        (self.pkg / "Game.inf").write_bytes(make_icon(["(icon by x)", "Slave=old.Slave", "PreLoad"]))
        (self.pkg / "Install").write_text(
            '(set #dest "WORK:Game") (makedir #dest) (copyfiles (source "Game.slave") (dest #dest)) '
            '(copyfiles (source "Game.inf") (newname "Game.info") (dest #dest)) '
            '(tooltype (dest (tackon #dest "Game")) (settooltype "Slave") (settooltype "PreLoad")) '
            '(tooltype (dest (tackon #dest "Game")) (settooltype "PreLoad" "") (settooltype "Slave" "Game.Slave")) '
            '(tooltype (dest (tackon #dest "Game")) (setdefaulttool "WHDLoad") (setstack 10240) (noposition))')
        res = ai.install(self.pkg, [self.disk1])
        icon = res.files["Game/Game.info"]
        tool, tts, _s, _e = ai.icon_info(icon)
        self.assertEqual((tool, tts), ("WHDLoad", ["(icon by x)", "PreLoad", "Slave=Game.Slave"]))
        self.assertEqual(struct.unpack_from(">I", icon, 74)[0], 10240)
        self.assertTrue(icon.endswith(b"TRAILER"))                         # (what follows - an OS3.5 image - is kept)

    def test_amigados_patterns(self) -> None:
        rx = ai.pattern_regex("~(#?.info|l)")
        self.assertTrue(rx.fullmatch("level1"))
        self.assertFalse(rx.fullmatch("game.INFO"))
        self.assertTrue(ai.pattern_regex("(intro|main)").fullmatch("Main"))
        self.assertTrue(ai.pattern_regex("BB.#?").fullmatch("bb.ani"))


AMIGA = platforms.get_platform("Commodore Amiga")
GAMES = "Commodore Amiga - Games - [ADF]"


class XadAndDelete(unittest.TestCase):
    def test_a_disk_image_file_that_cannot_be_read_is_left_out(self) -> None:
        img = bytearray(make_adf("Game", {"good": b"good data", "bad": b"x" * 600}))
        n = AdfDisk(bytes(img))._find("bad")
        first = struct.unpack_from(">I", img, n * BLOCK + BLOCK - 204)[0]       # (its first data block, overwritten as a trackloader
        img[first * BLOCK:first * BLOCK + 8] = b"\x4e\x75" * 4                   #  does - Project-X: no OFS data block header now)
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        pkg = tmp / "GameInstall"
        pkg.mkdir()
        (pkg / "Game.slave").write_bytes(b"SLAVE")
        (pkg / "image").write_bytes(bytes(img))
        (pkg / "Install").write_text(
            '(copyfiles (source "Game.slave") (dest "WORK:Game"))'
            ' (copyfiles (source "Game.slave") (dest "WORK:Game") (newname "disk.1"))'
            ' (copyfiles (source "Game.slave") (dest "WORK:Game") (newname "disk.2"))'
            ' (if (> 0 (run "xadUnFile image DEST WORK:Game/out DIMG")) (abort "xadUnFile failed"))'   # (as P_XADDecrunch looks at it)
            ' (if (<> 10 (run "xadUnFile image DEST WORK:Game/out DIMG")) (abort "not 10"))'
            ' (run "CD WORK:Game\nDelete disk.#? nomem Quiet")')
        res = ai.install(pkg, [make_adf("Other", {"x": b"x"})])
        self.assertEqual(res.files["Game/out/good"], b"good data")
        self.assertNotIn("Game/out/bad", res.files)
        self.assertTrue(any("left out: bad" in x for x in res.log))
        self.assertFalse([k for k in res.files if k.casefold().startswith("game/disk.")])   # (Delete took the pattern)
        self.assertIn("Game/Game.slave", res.files)


class PreferWhdloadVersions(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        env = mock.patch.dict(os.environ, {"ROMORG_DATA_DIR": str(self.tmp)})
        env.start()
        self.addCleanup(env.stop)
        whdready.clear_cache()
        self.addCleanup(whdready.clear_cache)

    def kept(self, names: list[str], **changes) -> list[str]:
        items = [Item(key=i, dat=GAMES, rom=Rom(name=n + ".adf", size=1, crc="", md5="", sha1="", game=n, dat=GAMES), style="tosec",
                      path=Path(n), member=None) for i, n in enumerate(names, 1)]
        sel = library.select(items, replace(library.default_profile(AMIGA), languages=(), **changes), AMIGA)
        return [n for k, n in enumerate(names, 1) if sel.decisions[k].action == KEEP]

    def test_the_version_whdload_can_install_is_kept(self) -> None:
        names = ["Zap (1993)(Acme)[cr FLT]", "Zap (1993)(Acme)[cr PDX]"]
        self.assertEqual(self.kept(names), ["Zap (1993)(Acme)[cr FLT]"])                 # (the usual order: the name)
        whdready.record({whdready.key_of(["Zap (1993)(Acme)[cr PDX].adf"]): {"ok": True, "archive": "Zap.lha", "reason": ""}})
        self.assertEqual(self.kept(names), ["Zap (1993)(Acme)[cr PDX]"])
        self.assertEqual(self.kept(names, prefer_whdload=False), ["Zap (1993)(Acme)[cr FLT]"])

    def test_the_shipped_list_knows_which_versions_boot(self) -> None:
        names = ["Zap (1993)(Acme)[cr FLT]", "Zap (1993)(Acme)[cr PDX]", "Zap (1993)(Acme)[cr QTX]"]
        key = lambda n: whdready.key_of([n + ".adf"])
        shipped = {key(names[0]): [whdready.FILES, "Zap.lha"], key(names[1]): [whdready.BOOTS, "Zap.lha"],
                   key(names[2]): [whdready.NO_BOOT, "Zap.lha"]}
        with mock.patch.object(whdready, "known", return_value=shipped):
            self.assertEqual(self.kept(names), ["Zap (1993)(Acme)[cr PDX]"])               # (the one that boots)
            whdready.record({key(names[0]): {"ok": False, "archive": "Zap.lha", "reason": "RawDIC"}})
            self.assertEqual(whdready.level([names[0] + ".adf"]), whdready.NONE)        # (your own check: it does not install)
            self.assertEqual(whdready.level([names[1] + ".adf"]), whdready.BOOTS)
            self.assertFalse(whdready.is_ready([names[2] + ".adf"]))                    # (installs, but does not boot)
            self.assertIsNone(whdready.level(["Other (1990)(X).adf"]))

    def test_the_shipped_list_is_in_the_package(self) -> None:
        sets = whdready.known()
        self.assertGreater(len(sets), 1000)
        self.assertTrue(all(isinstance(v, list) and v[0] in (-1, 0, 1, 2, 3) for v in list(sets.values())[:200]))

    def test_the_rule_is_on_by_default_for_the_amiga_only(self) -> None:
        self.assertTrue(library.default_profile(AMIGA).prefer_whdload)
        self.assertIn("prefer_whdload", library.applicable_fields(AMIGA))
        self.assertNotIn("prefer_whdload", library.applicable_fields(platforms.get_platform("Nintendo Game Boy Advance")))

    def test_an_original_that_asks_for_the_manual_gives_way_to_a_crack(self) -> None:
        sps, crack = "Commodore Amiga - Games - SPS", "Zap (1993)(Acme)[cr FLT]"
        items = [Item(key=1, dat=sps, rom=Rom(name="Zap (1993)(Acme)[0042] - Zap.ipf", size=1, crc="", md5="", sha1="aa",
                                              game="Zap (1993)(Acme)[0042]", dat=sps), style="tosec", path=Path("z.ipf"), member=None),
                 Item(key=2, dat=GAMES, rom=Rom(name=crack + ".adf", size=1, crc="", md5="", sha1="bb", game=crack, dat=GAMES),
                      style="tosec", path=Path("z.adf"), member=None)]
        orig, cr = whdready.key_of(["Zap (1993)(Acme)[0042] - Zap.ipf"]), whdready.key_of([crack + ".adf"])
        boots = {orig: [whdready.BOOTS, "Zap.lha"], cr: [whdready.BOOTS, "Zap.lha"]}

        class Db:
            def __init__(self, check: str) -> None:
                self.check = check

            def release(self, sha1s: list) -> dict:
                return {"protection": self.check} if "aa" in sha1s else {}

            games: dict = {}

            def titles(self) -> list:                                   # (the alternate titles read it too)
                return []

        def run(check: str = "Code Wheel", shipped: dict = boots, keeps: bool = True, **changes) -> Any:
            with mock.patch("romorg.openretro.database", return_value=Db(check) if check else None), \
                 mock.patch.object(whdready, "known", return_value=shipped), \
                 mock.patch.object(whdready, "leaves_manual", return_value=keeps):
                return library.select(items, replace(library.default_profile(AMIGA), languages=(), **changes), AMIGA).decisions[1]

        d = run()                                                       # (the install leaves the code wheel in; it boots the crack)
        self.assertEqual((d.action, d.superseded_by), (library.SUPERSEDED, crack))
        self.assertIn("asks for the manual (code wheel)", d.reason)
        self.assertEqual(run(keeps=False).action, KEEP)                 # (the install takes the check out)
        self.assertEqual(run(check="None").action, KEEP)                # (no check)
        self.assertEqual(run(check="").action, KEEP)                    # (no OpenRetro data)
        self.assertEqual(run(prefer_unprotected=False).action, KEEP)
        self.assertEqual(run(shipped={orig: [whdready.BOOTS, "Zap.lha"], cr: [whdready.NO_BOOT, "Zap.lha"]}).action, KEEP)   # (the crack does not boot)
        d = run(shipped={})                                             # (no install of the game known: any crack)
        self.assertEqual(d.action, library.SUPERSEDED)
        self.assertIn("no WHDLoad install", d.reason)
        d = run(shipped={orig: [whdready.BOOTS, "Zap.lha"], cr: [whdready.NO_BOOT, "Zap.lha"]}, keeps=False)
        self.assertEqual(d.action, KEEP)
        self.assertEqual(run(shipped={whdready.key_of(["Zap (1993)(Acme)[cr PDX].adf"]): [whdready.NONE, ""]}).action, KEEP)   # (an install, none booted)
        self.assertIn("prefer_unprotected", library.applicable_fields(AMIGA))
        self.assertTrue(library.default_profile(AMIGA).prefer_unprotected)
        self.assertNotIn("prefer_unprotected", library.applicable_fields(platforms.get_platform("Nintendo Game Boy Advance")))

    def test_the_language_rules_take_an_sps_release_by_its_name(self) -> None:
        sps = "Commodore Amiga - Games - SPS"
        mk = lambda k, game, f: Item(key=k, dat=sps, rom=Rom(name=f"{game} - {f}.ipf", size=1, crc="", md5="", sha1="", game=game, dat=sps),
                                     style="tosec", path=Path(f"{k}.ipf"), member=None)
        items = [mk(1, "Dune (1992)(Virgin)(DE)[0459]", "Dune_Disk1"), mk(2, "Dune (1992)(Virgin)[1156]", "Dune_Disk1")]
        with mock.patch.object(whdready, "known", return_value={}):              # (what WHDLoad runs: another rule - WhdloadOtherLanguageTests)
            sel = library.select(items, library.default_profile(AMIGA), AMIGA)      # (English, the default)
        self.assertEqual(sel.decisions[1].action, library.EXCLUDED)
        self.assertEqual(sel.decisions[2].action, KEEP)

    def test_the_shipped_list_names_installs_that_leave_the_manual_check(self) -> None:
        self.assertTrue(whdready.leaves_manual("CruiseForACorpse.lha"))      # (its ReadMe: "protection not removed")
        self.assertFalse(whdready.leaves_manual("Zool2.lha"))               # ("Manual protection removed")
        self.assertFalse(whdready.leaves_manual(None))

    def test_the_shipped_list_names_installs_that_need_a_newer_whdload_than_puae_s(self) -> None:
        self.assertEqual(whdready.needs_whdload("EmeraldMine.lha"), 20)      # (its slave: WHDLoad 20; PUAE has 19.1)
        self.assertEqual(whdready.needs_whdload("Zool2.lha"), 0)
        self.assertEqual(whdready.needs_whdload(None), 0)
        key = "Zeewolf v1.02 (1994)(Empire)[cr PDX].adf"
        self.assertEqual(whdready.known()[key], [whdready.BOOTS, "Zeewolf.lha"])   # (boots, with WHDLoad 20 in PUAE's WHDLoad/C)

    def test_the_versions_of_a_game_are_listed_for_the_check(self) -> None:
        names = ["Zap (1993)(Acme)(Disk 1 of 2)[cr FLT]", "Zap (1993)(Acme)(Disk 2 of 2)[cr FLT]", "Zap (1993)(Acme)[cr PDX]"]
        items = [Item(key=i, dat=GAMES, rom=Rom(name=n + ".adf", size=1, crc="", md5="", sha1="", game=n, dat=GAMES), style="tosec",
                      path=Path(n), member=None) for i, n in enumerate(names, 1)]
        out = library.variant_sets(items, replace(library.default_profile(AMIGA), languages=()), AMIGA)
        self.assertEqual(len(out), 1)
        title, sets = out[0]
        self.assertEqual(title, "Zap")
        self.assertEqual(sorted(len(s) for s in sets), [1, 2])


    def test_the_language_rules_take_an_unofficial_ipf_release_by_its_name(self) -> None:
        ipf = "Commodore Amiga - Games - [Unofficial IPF]"
        mk = lambda k, game, f: Item(key=k, dat=ipf, rom=Rom(name=f, size=1, crc="", md5="", sha1="", game=game, dat=ipf),
                                     style="tosec", path=Path(f"{k}.ipf"), member=None)
        de, en = "Alien Breed - Tower Assault (1994)(Team17)(DE)", "Alien Breed - Tower Assault (1994)(Team17)"
        items = [mk(1, de, f"{de}(Disk 1 of 2).ipf"), mk(2, de, f"{de}(Disk 2 of 2).ipf"),     # (its images' names start with the release's)
                 mk(3, en, f"{en}(Disk 1 of 2).ipf"), mk(4, en, f"{en}(Disk 2 of 2).ipf")]
        sel = library.select(items, library.default_profile(AMIGA), AMIGA)           # (English, the default)
        self.assertEqual([sel.decisions[k].action for k in (1, 2, 3, 4)], [library.EXCLUDED, library.EXCLUDED, KEEP, KEEP])
        self.assertEqual(sel.decisions[1].codes, ("language",))
        self.assertEqual({library._tags_of(it).title for it in items}, {"Alien Breed - Tower Assault"})   # (not "Alien Breed")
        self.assertIn(ipf, AMIGA.language_dats)


class OwnCheckByChecksum(unittest.TestCase):
    """The user's own check (``whdready.record``) found by the disks' checksums when a TOSEC update renames them."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        env = mock.patch.dict(os.environ, {"ROMORG_DATA_DIR": str(self.tmp)})
        env.start()
        self.addCleanup(env.stop)
        whdready.clear_cache()
        self.addCleanup(whdready.clear_cache)

    def test_a_renamed_disk_keeps_its_result(self) -> None:
        old = Rom(name="Zap (1993)(Acme).adf", size=1, crc="", md5="", sha1="0123456789abcdef9999", game="", dat=GAMES)
        new = Rom(name="Zap (1993)(Acme Software).adf", size=1, crc="", md5="", sha1="0123456789ABCDEF9999", game="", dat=GAMES)
        whdready.record({whdready.key_of([old.name]): {"ok": True, "archive": "Zap.lha", "reason": "", "sha1": whdready.sha_key([old])}})
        with mock.patch.object(whdready, "known", return_value={}):
            self.assertEqual(whdready.level_of([new]), whdready.FILES)                     # (by checksum: the same disk)
            self.assertIsNone(whdready.level([new.name]))                                  # (by name alone: never checked)
            other = Rom(name="Zap (1993)(Acme)[cr X].adf", size=1, crc="", md5="", sha1="fedcba9876543210", game="", dat=GAMES)
            self.assertIsNone(whdready.level_of([other]))


class ShippedListLookups(unittest.TestCase):
    """What the shipped list (``whdknown.json.gz``) answers: a version by its disks, an SPS release by its name, a title, an install."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        env = mock.patch.dict(os.environ, {"ROMORG_DATA_DIR": str(self.tmp)})
        env.start()
        self.addCleanup(env.stop)
        whdready.clear_cache()
        self.addCleanup(whdready.clear_cache)

    def test_an_sps_release_by_its_name(self) -> None:
        rel = "'Allo 'Allo! Cartoon Fun! (1993)(Alternative)[1005]"
        self.assertEqual(whdready.release(rel), [whdready.BOOTS, "AlloAllo.lha"])
        self.assertEqual(whdready.level([f"{rel} - AlloAlloCartoonFun_Disk2of2.ipf", f"{rel} - AlloAlloCartoonFun_Disk1of2.ipf"]),
                         whdready.BOOTS)                                             # (its images, in any order)
        self.assertIsNone(whdready.release(f"{rel} - AlloAlloCartoonFun_Disk1of2.ipf"))   # (an image's name is no release)
        self.assertIsNone(whdready.release("'Allo 'Allo! Cartoon Fun! (1993)(Alternative)(Disk 1 of 2)[cr CSL]"))   # (floppies: by their disks)
        self.assertIsNone(whdready.release("Zap (1993)(Acme)[0042]"))
        failed = whdready.release("A320 Airbus Vol. 2 v1.0 (1995)(Games 4 Europe)[1592]")
        self.assertEqual(failed, [whdready.NONE, ""])                               # (listed: its install was run, and did not install)

    def test_floppies_by_their_disks(self) -> None:
        ok = [f"'Allo 'Allo! Cartoon Fun! (1993)(Alternative)(Disk {n} of 2)[cr CSL].adf" for n in (2, 1)]
        self.assertEqual((whdready.level(ok), whdready.archive(ok), whdready.is_ready(ok)), (whdready.BOOTS, "AlloAllo.lha", True))
        self.assertIsNone(whdready.level(ok[:1]))                                    # (one disk of the set: another version)
        no_boot = ["1869 - History Experience Part I (1993)(Flair)(Disk 1 of 3)(A)[cr FLT].adf",
                   "1869 - History Experience Part I (1993)(Flair)(Disk 2 of 3)(B).adf",
                   "1869 - History Experience Part I (1993)(Flair)(Disk 3 of 3)(C).adf"]
        self.assertEqual((whdready.level(no_boot), whdready.archive(no_boot), whdready.is_ready(no_boot)),
                         (whdready.NO_BOOT, "1869.lha", False))                      # (installs, does not boot: its install known)
        none = ["1000cc Turbo (1990)(Energize)[cr CSL][a].adf"]
        self.assertEqual((whdready.level(none), whdready.archive(none), whdready.is_ready(none)), (whdready.NONE, None, False))
        whdready.record({whdready.key_of(no_boot): {"ok": True, "archive": "1869.lha", "reason": ""},
                         whdready.key_of(none): {"ok": True, "archive": "1000ccTurbo.lha", "reason": ""}})
        self.assertEqual(whdready.level(no_boot), whdready.NO_BOOT)                  # (whether it boots: only the shipped list knows)
        self.assertEqual(whdready.level(none), whdready.FILES)                       # (your own check: it installs here)

    def test_a_title_has_an_install(self) -> None:
        self.assertTrue(whdready.has_install("'Allo 'Allo! Cartoon Fun!"))
        self.assertTrue(whdready.has_install("'NAM 1965-1975 v1.0"))                  # (any case, without its version)
        self.assertTrue(whdready.has_install("A320 Airbus Vol. 2"))                   # (an SPS release's title)
        self.assertTrue(whdready.has_install("1000cc Turbo"))                         # (listed as 0: its game has an install, none installed)
        self.assertFalse(whdready.has_install("No Such Game At All"))

    def test_the_installs_that_leave_the_manual_check(self) -> None:
        self.assertTrue(whdready.leaves_manual("1000ccTurbo.lha"))
        self.assertFalse(whdready.leaves_manual("1869.lha"))
        self.assertFalse(whdready.leaves_manual(""))


if __name__ == "__main__":
    unittest.main()


class MoreInstallFixes(unittest.TestCase):
    """The failures of the 337 kept games with no WHDLoad build (Amendment 57)."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.pkg = self.tmp / "GameInstall"
        self.pkg.mkdir()
        (self.pkg / "Game.slave").write_bytes(b"SLAVE")
        (self.pkg / "Install.info").write_bytes(b"\x00\x00APPNAME=Game\x00")

    def install(self, script: str, disks: list, **kw):
        (self.pkg / "Install").write_text('(copyfiles (source "Game.slave") (dest "WORK:Game"))' + script)
        return ai.install(self.pkg, disks, **kw)

    def test_a_quoted_empty_argument_is_one(self) -> None:
        self.assertEqual(ai._split_args('assign WrathOfTheDemon: ""'), ["assign", "WrathOfTheDemon:", ""])   # (the current drawer)

    def test_dic_numbers_disks_from_0_when_the_install_does(self) -> None:
        disk = make_adf("Heimdall1", {"x": b"1"})
        res = self.install('(run "cd WORK:Game\\nDIC DF0: FD=0 LD=0")', [disk])                 # (Heimdall's disk.0: the 1st floppy)
        self.assertEqual(len(res.files["Game/Disk.0"]), 901120)

    def test_libs_of_the_users_workbench(self) -> None:
        wb = make_adf("Workbench3.1", {"libs/asl.library": b"ASL"})
        res = self.install('(copyfiles (source "LIBS:asl.library") (dest "WORK:Game/libs"))', [], system=ai.system_volumes(wb))
        self.assertEqual(res.files["Game/libs/asl.library"], b"ASL")                          # (Gunbee F99)

    def test_saved_settings_an_unused_original_lacks(self) -> None:
        disk = make_adf("Thomas Pinball - Disk 1", {"game": b"g"})
        res = self.install('(copyfiles (source "Thomas Pinball - Disk 1:prefs/Env-Archive/Sys/nonvolatile/Pinball") (dest "WORK:Game") (pattern "TT#?"))'
                           '(copyfiles (source "Thomas Pinball - Disk 1:game") (dest "WORK:Game"))', [disk])
        self.assertIn("Game/game", res.files)

    def test_a_volume_known_only_by_a_number_is_the_disk_that_has_the_file(self) -> None:
        disks = [make_adf(f"Ap2-Disk{n}", {"data": b"%d" % n}) for n in (1, 2, 3, 4)] + [make_adf("Ap2-Disk5", {"AP2": b"install"})]
        res = self.install('(askdisk (dest "Ap2-Install")) (copyfiles (source "Ap2-Install:AP2") (dest "WORK:Game"))', disks)
        self.assertEqual(res.files["Game/AP2"], b"install")                                     # (Abandoned Places 2)

    def test_the_disk_a_requester_asks_for(self) -> None:
        from romorg import amigados
        self.assertEqual(amigados.asked_disk("Insert your original disk #2"), 2)
        self.assertEqual(amigados.asked_disk("Insert Fears disk two in source drive."), 2)
        self.assertIsNone(amigados.asked_disk("Reading track 004."))

    def test_a_cd_default_is_never_the_answer_for_floppies(self) -> None:
        (self.pkg / "GameCD32.slave").write_bytes(b"CD32")         # (The Humans: "CD³² version" / "MFM disk version", default the CD)
        (self.pkg / "Install").write_text(
            '(set #v (askchoice (prompt "Which version would you like to install?") (default 0) (choices "CD³² version" "MFM disk version")))'
            '(if (= #v 0) (copyfiles (source "GameCD32.slave") (dest "WORK:Game") (newname "Game.slave"))'
            ' (copyfiles (source "Game.slave") (dest "WORK:Game")))')
        self.assertEqual(ai.install(self.pkg, [make_adf("Game1", {"x": b"1"})]).files["Game/Game.slave"], b"SLAVE")

    def test_a_package_file_the_slave_reads_and_the_script_forgets(self) -> None:
        import struct
        slave = bytearray(b"\x70\xff\x4e\x75WHDLOADS" + bytes(40) + b"data\0Game_v1.reloc\0")
        struct.pack_into(">H", slave, 26, 52)                       # (ws_CurrentDir "data"; Killing Cloud's v2.4 slave reads the reloc)
        (self.pkg / "Game.slave").write_bytes(bytes(slave))
        (self.pkg / "Game_v1.reloc").write_bytes(b"RELOC")
        files = {"Game/Game.slave": bytes(slave), "Game/data/Disk.1": b"d"}
        self.assertEqual(len(ai.repair_slave_files(files, self.pkg, "(copyfiles (source \"Game.slave\"))")), 1)
        self.assertEqual(files["Game/data/Game_v1.reloc"], b"RELOC")
        self.assertEqual(ai.repair_slave_files(files, self.pkg), [])          # (there now)

    def test_of_several_installs_the_one_for_these_disks(self) -> None:
        for name, label in (("SimCity1.2", "SimCity1.2"), ("SimCity512K", "SimCity512K")):   # (SimCity's package: an install a release)
            d = self.tmp / "pkg" / name
            d.mkdir(parents=True)
            (d / f"{name}.slave").write_bytes(name.encode())
            (d / "Install").write_text(f'(askdisk (dest "{label}")) (copyfiles (source "{name}.slave") (dest "WORK:{name}"))')
        files = {f"SimCityInstall/{p.relative_to(self.tmp / 'pkg')}": p.read_bytes() for p in (self.tmp / "pkg").rglob("*") if p.is_file()}
        ai.write_lha(self.tmp / "sim.lha", files)
        with ai.Package((self.tmp / "sim.lha").read_bytes()) as pkg:
            res = pkg.install([make_adf("SimCity512K", {"SimCity": b"exe"})])
        self.assertIn("SimCity512K/SimCity512K.slave", res.files)

    def test_unadf_unpacks_an_image_into_a_drawer_named_like_its_disk_and_t_is_never_installed(self) -> None:
        (self.pkg / "disk.1").write_bytes(make_adf("KQ1", {"Sierra": b"exe", "Data/vol.0": b"v0"}))   # (King's Quest: DIC's image in T:,
        res = self.install('(copyfiles (source "disk.1") (dest "T:"))'                                   #  UnADF into T:, copied from T:KQ1)
                           '(run "T:unadf T:disk.1 DEST=T:")'
                           '(copyfiles (source "T:KQ1") (dest "WORK:Game/data") (pattern "(data|sierra)"))', [])
        self.assertEqual(sorted(res.files), ["Game/Game.slave", "Game/data/Data/vol.0", "Game/data/Sierra", "load"])

    def test_a_drawer_is_renamed(self) -> None:
        disk = make_adf("ACS", {"ACS": b"exe", "c/x": b"c"})             # (Adventure Construction Set: "ACS" renamed "data", then ACS.EXE)
        res = self.install('(copyfiles (source "ACS:") (dest "WORK:Game/ACS") (all))'
                           '(rename "WORK:Game/ACS" "WORK:Game/data") (rename "WORK:Game/data/ACS" "WORK:Game/data/ACS.EXE")', [disk])
        self.assertEqual(sorted(k for k in res.files if k.startswith("Game/data")), ["Game/data/ACS.EXE", "Game/data/c/x"])
