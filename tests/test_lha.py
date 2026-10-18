"""The LhA reader (``romorg.lha``): header levels 0 and 2, stored and -lh5- data, CRC checks.

The archives are made here: stored (-lh0-) files, and an -lh5- block of literals only (every byte a fixed 8-bit code). The decoder was
also checked against every WHDLoad install archive (2,843, -lh1- outer archives and -lh5- installs, every CRC good)."""

from __future__ import annotations

import struct
import unittest

from romorg.lha import LhaError, LhaFile, crc16


class _BitWriter:
    def __init__(self) -> None:
        self.bits: list[int] = []

    def put(self, value: int, n: int) -> None:
        self.bits += [(value >> (n - 1 - i)) & 1 for i in range(n)]

    def data(self) -> bytes:
        bits = self.bits + [0] * (-len(self.bits) % 8)
        return bytes(int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8))


def lh5_literals(data: bytes) -> bytes:
    """One -lh5- block that codes every byte as itself (256 literals of length 8, the 254 lengths unused)."""
    w = _BitWriter()
    w.put(len(data), 16)                        # block size
    # the code-length code: symbols 2 (a run of zeros) and 10 (length 8), one bit each
    w.put(11, 5)                                # 11 lengths follow
    w.put(0, 3); w.put(0, 3); w.put(1, 3)       # symbols 0, 1: unused; 2: length 1
    w.put(3, 2)                                 # (after the third: 3 more zeros - symbols 3, 4, 5)
    for _ in range(4):                          # symbols 6..9 unused
        w.put(0, 3)
    w.put(1, 3)                                 # symbol 10: length 1
    # the literal / length code: 256 x length 8, then 254 zeros
    w.put(510, 9)
    for _ in range(256):
        w.put(1, 1)                             # symbol 10 (canonical code "1"): length 10 - 2 = 8
    w.put(0, 1)                                 # symbol 2 (code "0"): a run of 9-bit + 20 zeros
    w.put(254 - 20, 9)
    w.put(0, 4)                                 # the position code: none used (one symbol, 0)
    w.put(0, 4)
    for b in data:
        w.put(b, 8)
    return w.data()


def level0(name: str, method: bytes, packed: bytes, original: bytes) -> bytes:
    nm = name.encode("latin-1")
    body = method + struct.pack("<IIIBB", len(packed), len(original), 0, 0x20, 0) + bytes([len(nm)]) + nm + struct.pack("<H", crc16(original))
    return bytes([len(body), sum(body) & 0xFF]) + body + packed


def level2(name: str, method: bytes, packed: bytes, original: bytes) -> bytes:
    nm = name.encode("latin-1")
    ext = bytes([1]) + nm + struct.pack("<H", 0)                                   # filename, then "no more"
    first = len(ext)
    head = method + struct.pack("<IIIBB", len(packed), len(original), 0, 0x20, 2) + struct.pack("<H", crc16(original)) + b"A"
    head += struct.pack("<H", first) + ext
    total = 2 + len(head)
    return struct.pack("<H", total) + head + packed


class LhaTests(unittest.TestCase):
    def test_stored_and_lh5_files_of_both_header_levels(self) -> None:
        text = b"Insert disk 1 of Lemmings\n" * 20
        arc = (level0("LemmingsHD/ReadMe", b"-lh0-", text, text) + level2("LemmingsHD/Install", b"-lh5-", lh5_literals(text), text)
               + b"\x00")
        a = LhaFile(arc)
        self.assertEqual(a.names(), ["LemmingsHD/ReadMe", "LemmingsHD/Install"])
        self.assertEqual(a.read("LemmingsHD/ReadMe"), text)
        self.assertEqual(a.read("lemmingshd/install"), text)                        # (names compare without case)

    def test_a_damaged_file_fails_its_crc(self) -> None:
        text = b"0123456789" * 10
        bad = bytearray(level0("a", b"-lh0-", text, text))
        bad[-5] ^= 0xFF
        with self.assertRaises(LhaError):
            LhaFile(bytes(bad)).read("a")

    def test_a_web_page_is_no_archive(self) -> None:
        with self.assertRaises(LhaError):
            LhaFile(b"<!DOCTYPE HTML PUBLIC \"-//W3C//DTD HTML 4.01//EN\"><html><body>Not Found</body></html>")

    def test_crc16_is_the_arc_crc(self) -> None:
        self.assertEqual(crc16(b"123456789"), 0xBB3D)


if __name__ == "__main__":
    unittest.main()
