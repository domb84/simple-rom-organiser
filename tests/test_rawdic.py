"""RawDIC without an Amiga: the 68000 interpreter (``romorg.m68k``), RawDIC's library and state machine (``romorg.rawdic``) running an
imager, the IPF reader (``romorg.ipf``), and an install script that calls RawDIC (``romorg.amigainstall``)."""

from __future__ import annotations

import random
import shutil
import struct
import tempfile
import unittest
import zlib
from pathlib import Path

from romorg import amigainstall as ai, ipf, rawdic
from romorg.m68k import Cpu, CpuError
from tests.test_amigainstall import make_adf


def rawdic_crc(data: bytes) -> int:
    """slave.asm _TrackCRC16 step by step: moveq #-1,d0 / eor.b byte,d0 / 8 x (add.w d0,d0 / bcc / eor.w #$1021,d0)."""
    d0 = 0xFFFF
    for b in data:
        d0 = (d0 & 0xFF00) | ((d0 ^ b) & 0xFF)
        for _ in range(8):
            carry = d0 & 0x8000
            d0 = (d0 << 1) & 0xFFFF
            if carry:
                d0 ^= 0x1021
    return d0


def run_code(code: str, **regs: int) -> Cpu:
    cpu = Cpu(1 << 24)
    cpu.load(0x1000, bytes.fromhex(code.replace(" ", "") + "4e75"))
    for k, v in regs.items():
        (cpu.d if k[0] == "d" else cpu.a)[int(k[1])] = v
    cpu.call(0x1000, sp=0xF00000)
    return cpu


class CpuTests(unittest.TestCase):
    def test_an_mfm_decode_loop(self) -> None:
        # move.l (a0)+,d0 / move.l (a0)+,d3 / and.l d2,d0 / and.l d2,d3 / add.l d0,d0 / or.l d3,d0 / move.l d0,(a1)+ / dbf d1
        cpu = Cpu(1 << 24)
        cpu.load(0x1000, bytes.fromhex("2018 2618 c082 c682 d080 8083 22c0 51c9 fff0 4e75".replace(" ", "")))
        data = bytes(range(256)) * 4
        enc = b"".join(struct.pack(">HHHH", *(w for w, _r in rawdic._oddeven(data[i:i + 4]))) for i in range(0, len(data), 4))
        cpu.load(0x20000, enc)
        cpu.d[2], cpu.d[1], cpu.a[0], cpu.a[1] = 0x55555555, len(data) // 4 - 1, 0x20000, 0x40000
        cpu.call(0x1000, sp=0xF00000)
        self.assertEqual(bytes(cpu.m[0x40000:0x40000 + len(data)]), data)

    def test_flags(self) -> None:
        cpu = run_code("70ff 5280")                                   # moveq #-1,d0 / addq.l #1,d0
        self.assertEqual((cpu.d[0], cpu.z, cpu.c, cpu.x, cpu.n, cpu.v), (0, 1, 1, 1, 0, 0))
        cpu = run_code("203c 7fffffff 5280")                          # move.l #$7fffffff,d0 / addq.l #1,d0: overflow
        self.assertEqual((cpu.n, cpu.v, cpu.c), (1, 1, 0))
        cpu = run_code("7005 b03c 0007")                              # moveq #5,d0 / cmp.b #7,d0: 5 - 7 borrows
        self.assertEqual((cpu.c, cpu.n, cpu.z), (1, 1, 0))

    def test_bcd(self) -> None:
        cpu = run_code("7045 7238 c101", d0=0, d1=0)                  # moveq #$45,d0 / moveq #$38,d1 / abcd d1,d0
        self.assertEqual(cpu.d[0] & 0xFF, 0x83)
        cpu = run_code("7012 7229 8101")                              # $12 - $29 (sbcd d1,d0) = $83 and a borrow
        self.assertEqual((cpu.d[0] & 0xFF, cpu.c), (0x83, 1))

    def test_movem_and_subroutines(self) -> None:
        # movem.l d0-d2,-(sp) / moveq #0,d0 / moveq #0,d1 / moveq #0,d2 / bsr.s +2 / bra.s +2 / rts / movem.l (sp)+,d0-d2
        cpu = run_code("48e7 e000 7000 7200 7400 6102 6002 4e75 4cdf 0007", d0=1, d1=2, d2=3)
        self.assertEqual(cpu.d[:3], [1, 2, 3])
        self.assertEqual(cpu.a[7], 0xF00000)

    def test_68020_forms(self) -> None:
        cpu = Cpu(1 << 24)
        cpu.w32(0x2000 + 3 * 4, 0xCAFEBABE)
        cpu.load(0x1000, bytes.fromhex("2030 1c00 4e75"))              # move.l (0,a0,d1.l*4),d0
        cpu.a[0], cpu.d[1] = 0x2000, 3
        cpu.call(0x1000, sp=0xF00000)
        self.assertEqual(cpu.d[0], 0xCAFEBABE)
        cpu = run_code("4c01 0000", d0=100000, d1=300000)               # mulu.l d1,d0
        self.assertEqual(cpu.d[0], 30000000000 & 0xFFFFFFFF)
        cpu = run_code("4c41 0000", d0=1000001, d1=10)                  # divu.l d1,d0
        self.assertEqual(cpu.d[0], 100000)
        cpu = run_code("e9c0 1108", d0=0x12345678)                      # bfextu d0{4:8},d1
        self.assertEqual(cpu.d[1], 0x23)
        cpu = run_code("7080 49c0")                                     # moveq #-128,d0 / extb.l d0
        self.assertEqual(cpu.d[0], 0xFFFFFF80)

    def test_a_python_routine_is_called_like_a_subroutine(self) -> None:
        cpu = Cpu(1 << 24)
        cpu.hook(0x3000, lambda c: c.d.__setitem__(0, 42))
        cpu.load(0x1000, bytes.fromhex("4eb9 00003000 5280 4e75"))      # jsr $3000 / addq.l #1,d0
        cpu.call(0x1000, sp=0xF00000)
        self.assertEqual(cpu.d[0], 43)

    def test_what_it_does_not_do_is_an_error(self) -> None:
        with self.assertRaisesRegex(CpuError, "not one this interpreter does"):
            run_code("f200")                                            # (an FPU instruction)
        cpu = Cpu(1 << 24)
        cpu.load(0x1000, bytes.fromhex("60fe"))                         # bra.s * (for ever)
        with self.assertRaisesRegex(CpuError, "runs too long"):
            cpu.call(0x1000, sp=0xF00000, max_steps=10000)


# ---------------------------------------------------------------------- an imager, assembled here
class Asm:
    """Bytes with labels: 32-bit pointers (relocated, as in a hunk file) and 16-bit PC-relative displacements."""

    def __init__(self) -> None:
        self.b = bytearray()
        self.labels: dict[str, int] = {}
        self.ptrs: list[tuple[int, str]] = []
        self.rels: list[tuple[int, str]] = []

    def label(self, name: str) -> None:
        self.labels[name] = len(self.b)

    def hex(self, h: str) -> None:
        self.b += bytes.fromhex(h.replace(" ", ""))

    def raw(self, data: bytes) -> None:
        self.b += data

    def ptr(self, name: str) -> None:
        self.ptrs.append((len(self.b), name))
        self.b += bytes(4)

    def pcrel(self, name: str) -> None:
        self.rels.append((len(self.b), name))
        self.b += bytes(2)

    def align(self) -> None:
        self.b += bytes(-len(self.b) % 4)

    def hunks(self) -> bytes:
        for at, name in self.ptrs:
            struct.pack_into(">I", self.b, at, self.labels[name])
        for at, name in self.rels:
            struct.pack_into(">h", self.b, at, self.labels[name] - at)
        self.align()
        n = len(self.b) // 4
        out = struct.pack(">IIIIII", 0x3F3, 0, 1, 0, 0, n) + struct.pack(">II", 0x3E9, n) + bytes(self.b)
        out += struct.pack(">III", 0x3EC, len(self.ptrs), 0) + b"".join(struct.pack(">I", at) for at, _n in self.ptrs)
        return out + struct.pack(">II", 0, 0x3F2)


CUSTOM = 0x1800                                                     # (the game's own tracks: $1800 bytes, odd/even longs)


def make_imager(right_crc: int) -> bytes:
    a = Asm()
    a.hex("70ff 4e75")
    a.raw(b"RAWDIC")
    a.hex("05 00")                                                  # slave version 5, flags 0
    a.ptr("disk1"); a.ptr("text")
    for name, crc, alt in (("disk1", "crc1", "disk1b"), ("disk1b", "crc2", None)):
        a.label(name)
        a.hex("00000000 0001 0000"); a.ptr("tracks"); a.hex("00000000"); a.ptr("files"); a.ptr(crc)
        if alt:
            a.ptr(alt)
        else:
            a.hex("00000000")
        a.hex("00000000"); a.ptr("diskcode")
    a.label("tracks")
    a.hex("0000 0001 1600 4489 00000001")                           # tracks 0-1: standard (DMFM_STD)
    a.hex(f"0002 0003 {CUSTOM:04x} 4489"); a.ptr("decoder")         # tracks 2-3: the game's own format
    a.hex("ffff 0000")
    a.label("files")
    a.hex("00000000 00000000 ffffffff")                             # Disk.1, the whole image
    a.ptr("bootname"); a.hex("00000100 00000020")                   # boot.bin: $20 bytes at $100
    a.hex("ffffffff")
    a.label("crc1"); a.hex("0000 0bad ffff 0000")                   # a CRC that is not the disk's: the alternative is used
    a.label("crc2"); a.hex(f"0000 {right_crc:04x} ffff 0000")
    a.label("decoder")
    # move.l #$55555555,d2 / move.w #$5ff,d1 / loop: (as the CPU test) / dbf d1,loop / moveq #0,d0 / rts
    a.hex("243c 55555555 323c 05ff 2018 2618 c082 c682 d080 8083 22c0 51c9 fff0 7000 4e75")
    a.label("diskcode")
    a.hex("41fa"); a.pcrel("tailname")                              # lea tailname(pc),a0
    a.hex("203c 00002c00 223c 00000010 4ead 0018")                  # move.l #$2c00,d0 / move.l #$10,d1 / jsr AppendDiskFile(a5)
    a.hex("203c 00002c00 4ead 0018 7000 4e75")                      # (again: appended) / moveq #0,d0 / rts
    a.label("bootname"); a.raw(b"boot.bin\0")
    a.label("tailname"); a.raw(b"tail.bin\0")
    a.label("text"); a.raw(b"test imager\0")
    return a.hunks()


def make_drawer_imager() -> bytes:
    """An imager whose disk code makes a drawer with dos.library, saves a file in it, goes back (only when the old lock is not 0,
    as Bard's Tale's does) and saves another."""
    a = Asm()
    a.hex("70ff 4e75"); a.raw(b"RAWDIC"); a.hex("05 00"); a.ptr("disk"); a.ptr("text")
    a.label("disk")
    a.hex("00000000 0001 0000"); a.ptr("tracks"); a.hex("00000000 ffffffff 00000000 00000000 00000000"); a.ptr("code")
    a.label("tracks"); a.hex("0000 0001 1600 4489 00000001 ffff 0000")
    a.label("code")
    a.hex("2c78 0004 43fa"); a.pcrel("dosname")                     # move.l 4.w,a6 / lea dosname(pc),a1
    a.hex("7000 4eae fdd8 2c40 41fa"); a.pcrel("subname")           # moveq #0,d0 / OpenLibrary / move.l d0,a6 / lea subname(pc),a0
    a.hex("2208 4eae ff88 2200 4eae ff82 2e00 41fa"); a.pcrel("aname")  # CreateDir / CurrentDir / move.l d0,d7 / lea aname
    a.hex("43fa"); a.pcrel("text"); a.hex("7004 4ead 000c")          # lea text(pc),a1 / moveq #4,d0 / SaveFile
    a.hex("2207 6704 4eae ff82 41fa"); a.pcrel("bname")             # move.l d7,d1 / beq.s / CurrentDir / lea bname(pc),a0
    a.hex("43fa"); a.pcrel("text"); a.hex("7004 4ead 000c 7000 4e75")
    a.label("dosname"); a.raw(b"dos.library\0")
    a.label("subname"); a.raw(b"sub\0")
    a.label("aname"); a.raw(b"a\0")
    a.label("bname"); a.raw(b"b\0")
    a.label("text"); a.raw(b"text\0")
    return a.hunks()


class GameDisk:
    """Two standard tracks, then two in the game's own format (a sync, then the data as odd/even long pairs)."""

    def __init__(self, seed: int = 5) -> None:
        rnd = random.Random(seed)
        self.std = bytes(rnd.getrandbits(8) for _ in range(2 * 0x1600))
        self.own = [bytes(rnd.getrandbits(8) for _ in range(CUSTOM)) for _ in range(2)]

    def dos(self, n: int):
        return self.std[n * 0x1600:(n + 1) * 0x1600] if n in (0, 1) else None

    def track(self, n: int):
        if n in (0, 1):
            return rawdic.dos_track_mfm(self.dos(n), n)
        if n not in (2, 3):
            return None
        words = [(0, False)] * 8 + [(0x4489, True)]
        data = self.own[n - 2]
        for i in range(0, CUSTOM, 4):
            words += rawdic._oddeven(data[i:i + 4])
        words += [(0, False)] * 400
        return rawdic.mfm_encode(words), 16 * len(words)

    def image(self) -> bytes:
        return self.std + self.own[0] + self.own[1]


class ImagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.disk = GameDisk()
        self.islave = make_imager(rawdic_crc(self.disk.std[:0x1600]))

    def test_the_imager_images_the_disk(self) -> None:
        files = rawdic.Imager(self.islave).run({1: self.disk})
        img = self.disk.image()
        self.assertEqual(list(files), ["tail.bin", "Disk.1", "boot.bin"])      # (the disk code first, then the file list)
        self.assertEqual(files["Disk.1"], img)
        self.assertEqual(files["boot.bin"], img[0x100:0x120])
        self.assertEqual(files["tail.bin"], img[0x2C00:0x2C10] * 2)

    def test_a_disk_of_another_format_stops_it(self) -> None:
        plain = rawdic.AdfDisk(bytes(901120))                         # (standard tracks where the game's own are expected)
        with self.assertRaisesRegex(rawdic.ImagerError, "CRC|no sync|checksum"):
            rawdic.Imager(self.islave).run({1: plain})

    def test_a_missing_disk(self) -> None:
        with self.assertRaisesRegex(rawdic.ImagerError, "disk 1"):
            rawdic.Imager(self.islave).run({})

    def test_drawers_the_imager_makes(self) -> None:
        im = rawdic.Imager(make_drawer_imager())
        files = im.run({1: self.disk})
        self.assertEqual(files, {"sub/a": b"text", "b": b"text"})
        self.assertEqual(im.made_dirs, ["sub"])

    def test_mfm_round_trip(self) -> None:
        data = bytes(random.Random(1).getrandbits(8) for _ in range(0x1600))
        self.assertEqual(rawdic.dos_sectors(*rawdic.dos_track_mfm(data, 7)), data)
        self.assertIsNone(rawdic.dos_sectors(bytes(12668), 12668 * 8))


# ---------------------------------------------------------------------- IPF files, written here
def _rec(kind: bytes, body: bytes, extra: bytes = b"") -> bytes:
    h = bytearray(kind + struct.pack(">II", 12 + len(body), 0)) + body
    struct.pack_into(">I", h, 8, zlib.crc32(bytes(h)) & 0xFFFFFFFF)
    return bytes(h) + extra


_EVEN = [0] * 65536                                                 # an MFM word's 8 data cells
for _w in range(65536):
    _EVEN[_w] = sum(1 << (7 - k) for k in range(8) if _w & (1 << (14 - 2 * k)))


def _data_cells(mfm: bytes) -> bytes:
    return bytes(_EVEN[(mfm[i] << 8) | mfm[i + 1]] for i in range(0, len(mfm), 2))


def _elem(kind: int, payload: bytes, size: int) -> bytes:
    return bytes([(3 << 5) | kind]) + size.to_bytes(3, "big") + payload


def write_ipf(tracks: dict[int, tuple[bytes, int]], start: int = 0, split: bool = True) -> bytes:
    """An IPF (SPS encoder): with ``split`` each AmigaDOS sector a block (preamble and data as data elements, the sync pair as a
    sync element, the gap from a forward gap stream); else each track one raw element."""
    imges, datas = [], []
    for key, (n, (mfm, nbits)) in enumerate(sorted(tracks.items()), 1):
        blocks = []                                                 # (data bits, gap bits, data stream, gap stream)
        syncs = [i for i in range(0, len(mfm) - 4, 2) if mfm[i:i + 4] == b"\x44\x89\x44\x89"] if split else []
        if syncs:
            starts = [s - 4 for s in syncs]
            for k, s in enumerate(starts):
                e = starts[k + 1] if k + 1 < len(starts) else len(mfm)
                body = mfm[s:s + 1088]
                st = (_elem(2, _data_cells(body[:4]), 2) + _elem(1, body[4:8], 4) + _elem(2, _data_cells(body[8:]), 540)
                      + b"\x00")
                gap = (e - s - 1088) * 8
                blocks.append((1088 * 8, gap, st, bytes([(1 << 5) | 2, 8, 0x00, 0]) if gap else b""))
        else:
            blocks.append((nbits, 0, bytes([(3 << 5) | 4]) + nbits.to_bytes(3, "big") + mfm + b"\x00", b""))
        nb = len(blocks)
        gaps = b"".join(b[3] for b in blocks)
        descs, go, do = [], 32 * nb, 32 * nb + len(gaps)
        for db, gb, st, gs in blocks:
            flags = (1 if gs else 0) | (0 if split and syncs else 4)
            descs.append(struct.pack(">8I", db, gb, go, 1, 1, flags, 0, do))
            go += len(gs)
            do += len(st)
        extra = b"".join(descs) + gaps + b"".join(b[2] for b in blocks)
        dbits, gbits = sum(b[0] for b in blocks), sum(b[1] for b in blocks)
        imges.append(_rec(b"IMGE", struct.pack(">17I", n // 2, n % 2, 2, 1, (dbits + gbits) // 8, start // 8, start, dbits, gbits,
                                                dbits + gbits, nb, 0, 0, key, 0, 0, 0)))
        datas.append(_rec(b"DATA", struct.pack(">IIII", len(extra), 8 * len(extra), zlib.crc32(extra) & 0xFFFFFFFF, key), extra))
    info = struct.pack(">21I", 1, 2, 1, 0, 1, 0, 0, 79, 0, 1, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 0)
    return _rec(b"CAPS", b"") + _rec(b"INFO", info) + b"".join(imges) + b"".join(datas)


class IpfTests(unittest.TestCase):
    def test_the_tracks_come_out_as_written(self) -> None:
        data = bytes(random.Random(2).getrandbits(8) for _ in range(4 * 0x1600))
        tracks = {t: rawdic.dos_track_mfm(data[t * 0x1600:(t + 1) * 0x1600], t) for t in range(4)}
        disk = ipf.IpfDisk(write_ipf(tracks))                         # (sync elements, data elements, gap streams)
        for t in range(4):
            self.assertEqual(disk.track(t), tracks[t])
        self.assertIsNone(disk.track(9))
        turned = ipf.IpfDisk(write_ipf(tracks, start=1000))           # (the first block 1000 bits after the index)
        mfm, nbits = turned.track(1)
        v = int.from_bytes(tracks[1][0], "big")
        self.assertEqual(int.from_bytes(mfm, "big"), ((v >> 1000) | (v << (nbits - 1000))) & ((1 << nbits) - 1))
        self.assertEqual(rawdic.dos_sectors(mfm, nbits), data[0x1600:0x2C00])

    def test_a_damaged_file(self) -> None:
        blob = bytearray(write_ipf({0: rawdic.dos_track_mfm(bytes(0x1600), 0)}, split=False))
        blob[-20] ^= 0xFF
        with self.assertRaisesRegex(ipf.IpfError, "checksum"):
            ipf.IpfDisk(bytes(blob))
        with self.assertRaises(ipf.IpfError):
            ipf.IpfDisk(b"not an ipf")

    def test_gaps(self) -> None:
        # a block whose gap is: forward 16 data bits of $4E, backward the rest $00 (meeting in the gap)
        st = bytes([(1 << 5) | 1, 4]) + b"\x44\x89\x44\x89" + b"\x00"
        gs = bytes([(1 << 5) | 1, 16, (1 << 5) | 2, 8, 0x4E, 0]) + bytes([(1 << 5) | 2, 8, 0x00, 0])
        extra = struct.pack(">8I", 32, 96, 32, 1, 1, 3, 0, 32 + len(gs)) + gs + st
        blob = (_rec(b"CAPS", b"") + _rec(b"INFO", struct.pack(">21I", 1, 2, 1, *([0] * 18)))
                + _rec(b"IMGE", struct.pack(">17I", 0, 0, 2, 1, 16, 0, 0, 32, 96, 128, 1, 0, 0, 1, 0, 0, 0))
                + _rec(b"DATA", struct.pack(">IIII", len(extra), 8 * len(extra), zlib.crc32(extra) & 0xFFFFFFFF, 1), extra))
        mfm, nbits = ipf.IpfDisk(blob).track(0)
        self.assertEqual(nbits, 128)
        self.assertEqual(_data_cells(mfm[4:]), b"\x4e\x4e\x00\x00\x00\x00")
        self.assertEqual(mfm[:4], b"\x44\x89\x44\x89")


# ---------------------------------------------------------------------- an install that calls RawDIC
class InstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.pkg = self.tmp / "GameInstall"
        self.pkg.mkdir()
        (self.pkg / "Game.slave").write_bytes(b"SLAVE")
        self.disk = GameDisk()
        (self.pkg / "Game.islave").write_bytes(make_imager(rawdic_crc(self.disk.std[:0x1600])))
        (self.pkg / "Install").write_text(
            '(set #dest "WORK:Game") (makedir #dest) (copyfiles (source "Game.slave") (dest #dest))\n'
            '(copyfiles (source "Game.islave") (dest #dest))\n'
            '(run ("CD \\"%s\\"\\nRawDIC SLAVE=Game.islave SOURCE=DF0:\\nDelete Game.islave Quiet" #dest))\n'
            '(if (exists (tackon #dest "Disk.1")) ("") (abort "Disk imaging was not successful!"))\n')

    def test_rawdic_images_the_ipf(self) -> None:
        blob = write_ipf({t: self.disk.track(t) for t in range(4)}, split=False)
        res = ai.install(self.pkg, [blob])
        self.assertEqual(sorted(res.files), ["Game/Disk.1", "Game/Game.slave", "Game/boot.bin", "Game/tail.bin"])
        self.assertEqual(res.files["Game/Disk.1"], self.disk.image())
        self.assertTrue(any(line.startswith("RawDIC Game.islave") for line in res.log))

    def test_an_adf_where_the_game_has_its_own_format(self) -> None:
        adf = bytearray(901120)
        adf[:4] = b"DOS\x00"
        with self.assertRaisesRegex(ai.InstallError, "RawDIC \\(Game.islave\\)"):
            ai.install(self.pkg, [bytes(adf)])

    def test_an_ipf_of_a_standard_disk_is_read_as_an_adf(self) -> None:
        adf = make_adf("Game1", {"game": b"exe"})
        blob = write_ipf({t: rawdic.dos_track_mfm(adf[t * 0x1600:(t + 1) * 0x1600], t) for t in range(160)})
        disk = ai.floppy(blob)
        self.assertEqual(disk.data, adf)
        self.assertEqual(disk.read("game"), b"exe")
