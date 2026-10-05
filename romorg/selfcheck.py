"""``python -m romorg --self-check``: does everything the CHD engine needs work in THIS installation?

Run by ``packaging/build_appimage.sh`` against the freshly assembled AppDir and by ``packaging/smoke_test.sh``
against the built AppImage. It checks (and prints one line each)

* the bundled third-party tools are present (``tools/chdman``, ``tools/lib``, ``licenses/THIRD_PARTY.md``),
* the bundled chdman starts and prints its usage (a failure because the SYSTEM lacks a library, e.g. libSDL2 on a
  build container, is reported as ``WARN`` - the app then falls back, see :mod:`romorg.chdtool`),
* libFLAC loads through ctypes and decodes a synthetic hunk bit-identically to the pure-Python decoder,
* the parallel scheduler runs a tiny job (two worker processes, an uncompressed CD / GD image written here) and
  gives the sequential hashes,

and exits 1 when a required check fails. Outside an AppImage the bundle checks are skipped.
"""

from __future__ import annotations

import hashlib
import os
import struct
import sys
import tempfile
import zlib
from pathlib import Path
from typing import List, Tuple


def _crc8(data: bytes) -> int:
    crc = 0
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def _crc16(data: bytes) -> int:
    crc = 0
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x8005) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def flac_verbatim_frames(pcm_le: bytes, block: int = 588) -> bytes:
    """FLAC frames (verbatim subframes, independent stereo, 16 bit, 44.1 kHz) of little-endian stereo PCM."""
    vals = struct.unpack("<%dh" % (len(pcm_le) // 2), pcm_le)
    left, right = vals[0::2], vals[1::2]
    out = bytearray()
    for number, start in enumerate(range(0, len(left), block)):
        n = len(left[start:start + block])
        # sync 14 bits | reserved 0 | fixed block size 0 | blocksize code 7 (16 bit follows) | rate code 9 (44.1k)
        # channel assignment 1 (2 independent channels) | 16 bit (100) | 0 ; frame number (UTF-8) ; blocksize - 1
        head = bytes([0xFF, 0xF8, 0x79, 0x18, number & 0x7F]) + struct.pack(">H", n - 1)
        frame = bytearray(head + bytes([_crc8(head)]))
        for ch in (left, right):
            frame += b"\x02"                                  # subframe header: verbatim
            frame += struct.pack(">%dh" % n, *ch[start:start + n])
        frame += struct.pack(">H", _crc16(bytes(frame)))
        out += frame
    return bytes(out)


def _synthetic_pcm(samples: int) -> bytes:
    out = bytearray()
    v = 12345
    for i in range(samples):
        v = (v * 1103515245 + 12345) & 0x7FFFFFFF
        out += struct.pack("<hh", ((v >> 8) & 0xFFFF) - 32768, (i * 37) % 65536 - 32768)
    return bytes(out)


def write_tiny_chd(path: Path, data_frames: int = 40, audio_frames: int = 40, gd: bool = True) -> Tuple[bytes, bytes]:
    """An UNCOMPRESSED CD / GD-ROM CHD v5 (data track + audio track); returns the two tracks' extracted bytes."""
    unit = 2448
    hunk = unit * 2
    sec = bytearray()
    t1 = bytearray()
    for i in range(data_frames):
        s = bytes([(i * 7 + j) & 0xFF for j in range(2352)])
        t1 += s
        sec += s + bytes(96)
    pcm = _synthetic_pcm(audio_frames * 588)
    be = bytearray(pcm)
    be[0::2], be[1::2] = pcm[1::2], pcm[0::2]               # hunks hold CD audio big-endian
    for i in range(audio_frames):
        sec += bytes(be[i * 2352:(i + 1) * 2352]) + bytes(96)
    frames = data_frames + audio_frames
    nh = (frames + 1) // 2
    sec += bytes(nh * hunk - len(sec))
    base = -(-(124 + 4 * nh) // hunk) * hunk
    meta_off = base + nh * hunk
    tag = b"CHGD" if gd else b"CHT2"
    texts = []
    for n, (typ, fr) in enumerate((("MODE1_RAW", data_frames), ("AUDIO", audio_frames)), 1):
        texts.append(f"TRACK:{n} TYPE:{typ} SUBTYPE:NONE FRAMES:{fr} PAD:0 PREGAP:0 PGTYPE:MODE1 PGSUB:NONE POSTGAP:0"
                     .encode() + b"\0")
    meta = bytearray()
    for i, t in enumerate(texts):
        nxt = meta_off + len(meta) + 16 + len(t) if i + 1 < len(texts) else 0
        meta += tag + b"\x01" + len(t).to_bytes(3, "big") + struct.pack(">Q", nxt) + t
    raw = bytes(sec)
    header = bytearray(b"MComprHD" + struct.pack(">II", 124, 5) + b"\0" * 16)
    header += struct.pack(">QQQII", len(raw), 124, meta_off, hunk, unit)
    header += hashlib.sha1(raw).digest() + hashlib.sha1(raw).digest() + b"\0" * 20
    mp = b"".join(struct.pack(">I", base // hunk + i) for i in range(nh))
    path.write_bytes(bytes(header) + mp + bytes(base - 124 - len(mp)) + raw + bytes(meta))
    return bytes(t1), pcm


def check_flac() -> Tuple[bool, str]:
    from . import flacdec, flacnative
    st = flacnative.status()
    if not st["native"]:
        return False, f"libFLAC not available ({st['note']}) - the slow pure-Python FLAC decoder is used"
    pcm = _synthetic_pcm(588 * 8)
    stream = flac_verbatim_frames(pcm) + b"\x78" * 20          # + trailing "subcode" bytes
    got, end = flacnative.decode_frames(stream, 0, 588 * 8)
    ref, end2 = flacdec.decode_frames(stream, 0, 588 * 8)
    if got != ref or end != end2 or got.tobytes() != pcm:
        return False, "libFLAC output differs from the pure-Python decoder"
    return True, f"libFLAC {st['library']} decodes a synthetic hunk bit-identically (consumed {end} bytes)"


def check_zstd() -> Tuple[str, str]:
    """``("OK" | "WARN", text)``: Zstandard CHDs (cdzs / zstd) are fast with a library and slow without one."""
    from . import zstdnative
    st = zstdnative.status()
    if not st["native"]:
        if zstdnative.decompress(bytes.fromhex("28b52ffd2005" "290000") + b"romor", 5) != b"romor":
            return "WARN", "the built-in Zstandard decoder did not decode a test frame"
        return "WARN", (f"no Zstandard library ({st['note']}): cdzs / zstd CHDs are decoded by the built-in "
                        "Python decoder (correct, about 1 MB/s)")
    # a hand-made frame: magic, single-segment header with content size 5, one last raw block holding "romor"
    frame = bytes.fromhex("28b52ffd2005" "290000") + b"romor"
    if zstdnative.decompress(frame, 5) != b"romor":
        return "WARN", "the Zstandard library did not decode a test frame"
    return "OK", f"Zstandard CHDs (cdzs / zstd) are decoded with {st['library']}"


def check_scheduler() -> Tuple[bool, str]:
    from . import chd as chdlib
    from . import chdsched
    with tempfile.TemporaryDirectory(prefix="romorg-selfcheck-") as tmp:
        path = Path(tmp) / "tiny.chd"
        t1, pcm = write_tiny_chd(path)
        with chdlib.Chd(path) as info:
            want = {0: hashlib.sha1(t1).hexdigest(), 1: hashlib.sha1(pcm).hexdigest()}
            seq = {i: chdlib.hash_track(info, t).sha1 for i, t in enumerate(info.tracks)}
            if seq != want:
                return False, "the sequential reader does not reproduce the synthetic tracks"
            with chdsched.Scheduler(2, chunk_bytes=256 << 10) as sched:
                got = sched.hash_tracks(info, [0, 1])
                if {i: h["sha1"] for i, h in got.items()} != want:
                    return False, "the scheduler's hashes differ from the sequential ones"
                crc = {i: h["crc32"] for i, h in got.items()}
                if crc[0] != "%08x" % (zlib.crc32(t1) & 0xFFFFFFFF):
                    return False, "wrong crc32 from the scheduler"
                workers = sched.workers
    return True, f"the scheduler hashed a tiny GD image with {workers} worker processes (identical to sequential)"


def check_chdman() -> Tuple[str, str]:
    """``("OK" | "WARN" | "SKIP", text)``"""
    from . import bundle, chdtool
    c = chdtool.bundled_chdman()
    if c is None:
        return "SKIP", "no bundled chdman (not an AppImage build)"
    ok, why = chdtool._probe_detail(c.argv, c.environ())
    if ok:
        return "OK", f"the bundled chdman starts ({c.argv[0]})"
    return "WARN", f"the bundled chdman cannot start on this system: {why} (the app falls back to other chdman installs)"


def main(argv: List[str] | None = None) -> int:
    from . import bundle
    failures = 0
    root = bundle.bundle_root()
    lines: List[Tuple[str, str]] = []
    if root is not None:
        need = [root / "tools" / "chdman", root / "licenses" / "THIRD_PARTY.md"]
        libs = list((root / "tools" / "lib").glob("libFLAC.so*")) if (root / "tools" / "lib").is_dir() else []
        missing = [str(p) for p in need if not p.exists()] + ([] if libs else ["tools/lib/libFLAC.so*"])
        if missing:
            lines.append(("FAIL", "bundle incomplete: missing " + ", ".join(missing)))
        else:
            lines.append(("OK", f"bundle at {root}: tools/chdman, tools/lib/libFLAC, licenses/THIRD_PARTY.md"))
    else:
        lines.append(("SKIP", "not running from an AppImage (no bundle)"))
    status, text = check_chdman()
    lines.append((status, text))
    try:
        lines.append(check_zstd())
    except Exception as exc:  # noqa: BLE001
        lines.append(("WARN", f"Zstandard check: {type(exc).__name__}: {exc}"))
    for fn in (check_flac, check_scheduler):
        try:
            ok, text = fn()
        except Exception as exc:  # noqa: BLE001
            ok, text = False, f"{type(exc).__name__}: {exc}"
        # inside the bundle libFLAC is REQUIRED; elsewhere its absence only means the slow decoder
        required = fn is check_scheduler or root is not None
        lines.append(("OK" if ok else ("FAIL" if required else "WARN"), text))
    for status, text in lines:
        print(f"{status:5} {text}")
        failures += status == "FAIL"
    print("SELF-CHECK " + ("FAILED" if failures else "PASSED"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
