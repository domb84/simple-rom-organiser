"""The one place where a Switch file is looked into with a key: the header of an NCA, to read the title ID of a game card dump.

Only used when the title cannot be told otherwise (an XCI whose name has no ``[title ID]`` and whose NCAs no database knows), and
only with the ``prod.keys`` the user's own emulator uses. Only the 512 bytes of one sector of one small NCA are decrypted (AES-128-XTS
with the ``header_key``); nothing of the game is. Pure Python (stdlib only): a sector is 32 AES blocks.

The decrypted header is checked by its magic (``NCA2`` / ``NCA3``), so a wrong key never gives a wrong ID.
"""

from __future__ import annotations

import os
import struct
from pathlib import Path
from typing import Dict, Iterable, List, Optional

__all__ = ["load_keys", "find_prod_keys", "nca_title_id", "aes_decrypt_block", "aes_encrypt_block", "xts_decrypt_sector"]


# --------------------------------------------------------------------------- AES-128 (FIPS 197)
def _tables() -> tuple[list[int], list[int]]:
    sbox = [0] * 256
    p = q = 1
    while True:
        p = (p ^ ((p << 1) & 0xFF) ^ (0x1B if p & 0x80 else 0)) & 0xFF         # p * 3
        q ^= (q << 1) & 0xFF
        q ^= (q << 2) & 0xFF
        q ^= (q << 4) & 0xFF
        if q & 0x80:
            q ^= 0x09                                                          # q / 3
        x = q ^ _rotl8(q, 1) ^ _rotl8(q, 2) ^ _rotl8(q, 3) ^ _rotl8(q, 4)
        sbox[p] = (x ^ 0x63) & 0xFF
        if p == 1:
            break
    sbox[0] = 0x63
    inv = [0] * 256
    for i, v in enumerate(sbox):
        inv[v] = i
    return sbox, inv


def _rotl8(x: int, n: int) -> int:
    return ((x << n) | (x >> (8 - n))) & 0xFF


_SBOX, _INV = _tables()


def _xtime(a: int) -> int:
    a <<= 1
    return (a ^ 0x11B) & 0xFF if a & 0x100 else a


def _mul(a: int, b: int) -> int:
    out = 0
    while b:
        if b & 1:
            out ^= a
        a = _xtime(a)
        b >>= 1
    return out


_M9, _M11, _M13, _M14 = ([_mul(i, k) for i in range(256)] for k in (9, 11, 13, 14))


def _expand(key: bytes) -> List[List[int]]:
    if len(key) != 16:
        raise ValueError("an AES-128 key is 16 bytes")
    words = [list(key[i:i + 4]) for i in range(0, 16, 4)]
    rcon = 1
    for i in range(4, 44):
        t = list(words[i - 1])
        if i % 4 == 0:
            t = [_SBOX[b] for b in t[1:] + t[:1]]
            t[0] ^= rcon
            rcon = _xtime(rcon)
        words.append([a ^ b for a, b in zip(words[i - 4], t)])
    return [sum(words[r * 4:r * 4 + 4], []) for r in range(11)]


def aes_encrypt_block(key: bytes, block: bytes) -> bytes:
    rk = _expand(key)
    s = [b ^ k for b, k in zip(block, rk[0])]
    for r in range(1, 11):
        s = [_SBOX[b] for b in s]
        s = [s[(i + 4 * (i % 4)) % 16] for i in range(16)]                      # ShiftRows
        if r < 10:
            m = []
            for c in range(0, 16, 4):
                a0, a1, a2, a3 = s[c:c + 4]
                m += [_xtime(a0) ^ _xtime(a1) ^ a1 ^ a2 ^ a3, a0 ^ _xtime(a1) ^ _xtime(a2) ^ a2 ^ a3,
                      a0 ^ a1 ^ _xtime(a2) ^ _xtime(a3) ^ a3, _xtime(a0) ^ a0 ^ a1 ^ a2 ^ _xtime(a3)]
            s = m
        s = [b ^ k for b, k in zip(s, rk[r])]
    return bytes(s)


def aes_decrypt_block(key: bytes, block: bytes) -> bytes:
    rk = _expand(key)
    s = [b ^ k for b, k in zip(block, rk[10])]
    for r in range(9, -1, -1):
        s = [s[(i - 4 * (i % 4)) % 16] for i in range(16)]                      # InvShiftRows
        s = [_INV[b] for b in s]
        s = [b ^ k for b, k in zip(s, rk[r])]
        if r > 0:
            m = []
            for c in range(0, 16, 4):
                a0, a1, a2, a3 = s[c:c + 4]
                m += [_M14[a0] ^ _M11[a1] ^ _M13[a2] ^ _M9[a3], _M9[a0] ^ _M14[a1] ^ _M11[a2] ^ _M13[a3],
                      _M13[a0] ^ _M9[a1] ^ _M14[a2] ^ _M11[a3], _M11[a0] ^ _M13[a1] ^ _M9[a2] ^ _M14[a3]]
            s = m
    return bytes(s)


# --------------------------------------------------------------------------- AES-XTS with Nintendo's sector tweak
def _times_alpha(t: bytes) -> bytes:
    n = int.from_bytes(t, "little") << 1
    if n >> 128:
        n = (n & ((1 << 128) - 1)) ^ 0x87
    return n.to_bytes(16, "little")


def xts_decrypt_sector(key: bytes, data: bytes, sector: int) -> bytes:
    """Decrypt one sector (a multiple of 16 bytes) with a 32-byte XTS key; the tweak is the sector number, big endian."""
    if len(key) != 32 or len(data) % 16:
        raise ValueError("a 32-byte key and whole blocks")
    k1, k2 = key[:16], key[16:]
    t = aes_encrypt_block(k2, sector.to_bytes(16, "big"))
    out = bytearray()
    for i in range(0, len(data), 16):
        block = bytes(a ^ b for a, b in zip(data[i:i + 16], t))
        plain = aes_decrypt_block(k1, block)
        out += bytes(a ^ b for a, b in zip(plain, t))
        t = _times_alpha(t)
    return bytes(out)


# --------------------------------------------------------------------------- keys and NCA headers
def load_keys(path: Path) -> Dict[str, bytes]:
    """The ``name = hex`` lines of a ``prod.keys`` (only the names this module needs are kept)."""
    keys: Dict[str, bytes] = {}
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return keys
    for line in text.splitlines():
        name, sep, value = line.partition("=")
        if sep and name.strip() == "header_key":
            try:
                keys["header_key"] = bytes.fromhex(value.strip())
            except ValueError:
                pass
    return keys


def find_prod_keys(extra: Iterable[Path] = (), home: Optional[Path] = None) -> Optional[Path]:
    """The first ``prod.keys`` that exists in the given folders or the usual places of Eden, yuzu, Sudachi and Ryujinx."""
    home = Path(home) if home else Path.home()
    cands: List[Path] = [Path(p) / "prod.keys" for p in extra]
    for share in (home / ".local" / "share", home / ".config", home / "AppData" / "Roaming"):
        for app in ("eden", "yuzu", "sudachi", "suyu", "Ryujinx"):
            cands += [share / app / "keys" / "prod.keys", share / app / "system" / "prod.keys"]
    appdata = os.environ.get("APPDATA")
    if appdata:
        cands += [Path(appdata) / app / "keys" / "prod.keys" for app in ("eden", "yuzu")] + [Path(appdata) / "Ryujinx" / "system" / "prod.keys"]
    for c in cands:
        if c.is_file():
            return c
    return None


def nca_title_id(f, offset: int, header_key: bytes) -> Optional[str]:
    """The title ID in the header of the NCA that starts at ``offset`` of the open file ``f``; None when the key does not open it."""
    f.seek(offset + 0x200)
    raw = f.read(0x200)
    if len(raw) < 0x200 or len(header_key) != 32:
        return None
    plain = xts_decrypt_sector(header_key, raw, 1)
    if plain[:4] not in (b"NCA2", b"NCA3"):
        return None
    return f"{struct.unpack('<Q', plain[0x10:0x18])[0]:016X}"
