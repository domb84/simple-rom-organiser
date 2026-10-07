"""``python -m romorg --self-check``: does everything the CHD engine needs work in THIS installation?

Run by ``packaging/build_appimage.sh`` against the freshly assembled AppDir and by ``packaging/smoke_test.sh``
against the built AppImage. It checks (and prints one line each)

* the bundled third-party tools are present (``tools/chdman``, ``tools/lib``, ``licenses/THIRD_PARTY.md``),
* the bundled chdman starts and prints its usage (a failure because the SYSTEM lacks a library, e.g. libSDL2 on a
  build container, is reported as ``WARN`` - the app then falls back, see :mod:`romorg.chdtool`),
* libFLAC loads through ctypes and decodes a synthetic hunk bit-identically to the pure-Python decoder,
* inside an AppImage libFLAC AND libzstd must have been loaded from the bundle (``tools/lib``), never from the
  system: the app must not depend on what the host (SteamOS: an immutable image) happens to ship,
* the parallel scheduler runs a tiny job (two worker processes, an uncompressed CD / GD image written here) and
  gives the sequential hashes,

and exits 1 when a required check fails. Outside an AppImage the bundle checks are skipped.

Inside a Windows package (the single-file exe, or the .zip whose ``app\\native`` folder holds the DLLs) the package
check (:func:`check_windows_package`) replaces the AppImage one: a libFLAC that ships must be the one loaded and must
encode, Zstandard must come from ``compression.zstd`` (or another library), and a libsndfile that ships must load.

``--require-native`` (the Windows builds, ``packaging/build_windows*.ps1``) makes what a shipped package must bring
REQUIRED: libFLAC must load, Zstandard must come from a library (``compression.zstd`` / libzstd), and the writer must
run in worker processes and store the audio track as FLAC (``cdfl``).
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
    from . import flacdec, flacnative, nativeflac
    st = flacnative.status()
    if not st["native"]:
        return False, (f"no libFLAC and no libsndfile ({st['note']}) - CD audio is decoded by the slow pure-Python "
                       "FLAC decoder")
    pcm = _synthetic_pcm(588 * 8)
    stream = flac_verbatim_frames(pcm) + b"\x78" * 20          # + trailing "subcode" bytes
    if st["library"] == "libsndfile":       # libFLAC missing; libsndfile decodes hunks whose subcode is not wanted
        got = nativeflac.decode_frames(flac_verbatim_frames(pcm), 588 * 8)
        if got != flacdec.decode_frames(stream, 0, 588 * 8)[0]:
            return False, "libFLAC not available, and libsndfile's output differs from the pure-Python decoder"
        return False, ("libFLAC not available: CD audio is decoded through libsndfile (checked bit-identical); "
                       "hunks with subcode use the slow pure-Python decoder and the writer stores audio without FLAC")
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


def _in_dir(path: str, folder: Path) -> bool:
    try:
        return Path(path).resolve().parent == folder.resolve()
    except (OSError, ValueError):
        return False


def check_bundled_libraries(root: Path) -> List[Tuple[str, str]]:
    """Inside an AppImage libFLAC and libzstd must be the copies in ``<root>/tools/lib`` (``FAIL`` otherwise).

    A Python that has Zstandard itself (3.14 ``compression.zstd``) satisfies the libzstd requirement. libsndfile,
    the system's libFLAC or libzstd, or the pure-Python fallbacks are all failures here: they work, but only by
    accident of what the host ships."""
    from . import flacnative, zstdnative
    lib = root / "tools" / "lib"
    out: List[Tuple[str, str]] = []
    flac = flacnative.library_path()
    if flac and _in_dir(flac, lib):
        out.append(("OK", f"libFLAC {flac} is loaded from the bundle"))
    else:
        out.append(("FAIL", f"libFLAC was not loaded from the bundle ({lib}): "
                            + (f"it came from {flac}" if flac else f"none loaded ({flacnative.status()['note']})")))
    st = zstdnative.status()
    zlib_ = st["library"] if st["native"] else None
    if zlib_ == "compression.zstd":
        out.append(("OK", "libzstd is not needed: this Python has compression.zstd built in"))
    elif zlib_ and _in_dir(zlib_, lib):
        out.append(("OK", f"libzstd {zlib_} is loaded from the bundle"))
    else:
        out.append(("FAIL", f"libzstd was not loaded from the bundle ({lib}): "
                            + (f"it came from {zlib_}" if zlib_ else f"none loaded ({st['note']})")))
    return out


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
                if not sched.pooled or not sched.stats["chunks"]:
                    return False, "the scheduler did not use its worker processes"
                workers = sched.workers
    return True, f"the scheduler hashed a tiny GD image with {workers} worker processes (identical to sequential)"


# A 256 KiB GameCube image as an RVZ (Zstandard, 1.4 KB): header + 1 KB of data, a zero group, a group of padding
# regenerated from a seed (the generator is the part that must work), zeros. Made by tests/rvztestlib.build_rvz.
_TINY_RVZ = (
    "UlZaAQEAAAAAAwAAAAAA3ID93XfihbGG/0b1ntpxLq+Au7etAAAAAAAEAAAAAAAAAAAFZHgkMX4T9yV2kKQwcEF1gmXKKBG7"
    "AAAAAQAAAAUAAAADAAIAAEdUU1QwMQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAVGVzdCBEaXNjAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAADAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAQAAAAAAAAUoAAAAGwAAAAIAAAAAAAAFQwAAACEAAAAA"
    "AAAAACi1L/2gAAACAI0cAFQ4AAD1OlkcEJOISa1FF9E66RkZUpQcc5nu8AKWSZTQBzIW5GHUZ693boN9EW/srQ6VcVCASNE1"
    "LQqwZbwpY0DJGgBZCInb+gmDuP8MveftEoBMW+BiHc7KHFhgq3bp/2ji9741eXqDjHNBo3KziRPZ3R25VRkkV4FI9BYPFLWO"
    "HJy+icB/yErcR6BJg0wbdVhCESQkp2Dgu0kRpbW9N7QQ/2a3cATSkx2KGk+WUFUGiQsJ2eAS+7WSmWxenRoRXULZCHYpchZs"
    "chQ3FycCFmbNhhbkUZrxlSVf8I2YwJp/4XkC3kyIzx1m2ulsBbqggQDec826ngnAYaPj3fBtpJk+aBzgTTC0feQN7WWbbrDq"
    "86NPhqZ0NkLxsC/YyeaCa+NPYpAWnR3tD551+58xrHo/p1CDtc+ahNnTH878DH8GQAhVkyN2ejm1MQ3EnhEGIb0ccdGG54MM"
    "myl19lTFZLTghPGFxPAG4w7HpKseckIaT3ehtyuwT59qyO1tFJBUeDwmdSTGw2t7SuNh4jn6mjpZRYHZk7vL4rJCPB9pem2g"
    "HI5hs/Wa8HTfIZ+sa/4qePX/6rbaZfHtX8qp9WfRuvwsAJf6Zo6ui+71QUBPy06PV7zuZlf+dKtqKov/VKnATl92U4zs0r/M"
    "29CYpTB+fIAjVcWyrLmYeoeNDc5KUmviJxHmZth33W4IGNkzlRpcS+6Y89QTit9EfPhfAlXZeQjqKFwmUTPYp+h7zeG35eaY"
    "cozu9fRkTnWIQLXNaArchbgZTe1XBVF9rsEkXKkP6OsWjigzUJqWQv9fUbFIQk58VcZ2dyNh4oKMq0bEcxuA05awRX9Ea9bO"
    "8akpb7B4zo58MPWpfZlC6CNRmwh40tbU5PhBSqdwEFF56ih11iu06mTD2M+pCS8Zz1M+cNsieLDDwG23VkT7yAAGVPZaZuKD"
    "JcJCQmqc8nOxQwbsNdEb98FJ3C6C3J738LD/bAgiqtdch1y6FlSper7erPCwpXUQ80EHpvMfN0TqSTznpoCYf2DCwF504L4Y"
    "nG3QMeKFX7+SlEzb1qpLaSTDdyXciQtztXqe6h2jlwMGXZgX0AUOAJbUg3Oc0qSUZVvti049o+4IXhvW78hDFTC5MZzqjDTL"
    "TAPkMiZJcj/8/O0e2r/+nvPGbgvUkXnAfw7ki+x11PTOzb5EIlEhqysZ9sFqIaqiGMd0WwAAAAIAg/X3QZ52CtoAAAAotS/9"
    "oEyQAQCtAgDEBIAAcAA905LMAtCyQN+VblMGMYMyrPuQPbL6cDFqBZPSL7KN8By9dB28z9VKI6fqeeio+vNdMNKO7j/s/+NS"
    "gJyMJuE8MJ49JQABkAABAEz/Yw6iAAAAKLUv/SAYlQAASAAAgAAD/wAAAgNAAisD4gIBKLUv/SAYwQAAAAAASYAAA50AAAAA"
    "AAABMYAAAGEAAZBM"
)
_TINY_RVZ_SHA1 = "8e7dedcbe0c3b2f022991e89e4cf40ee435784b9"
_TINY_RVZ_CRC32 = "312c288f"


def check_rvz() -> Tuple[bool, str]:
    """The GameCube reader rebuilds a tiny RVZ (Zstandard + regenerated padding) into the exact image."""
    import base64
    import io
    from . import rvz, zstdnative
    blob = base64.b64decode("".join(_TINY_RVZ))
    with rvz.Rvz("selfcheck.rvz", fileobj=io.BytesIO(blob)) as r:
        image = b"".join(r.iter_image())
    if hashlib.sha1(image).hexdigest() != _TINY_RVZ_SHA1 or "%08x" % (zlib.crc32(image) & 0xFFFFFFFF) != _TINY_RVZ_CRC32:
        return False, "the RVZ reader did not rebuild the test image exactly"
    st = zstdnative.status()
    how = st["library"] if zstdnative.native() else "the built-in Python Zstandard decoder"
    return True, f"the RVZ reader rebuilt a test GameCube image exactly (Zstandard through {how})"


def check_writer(require_native: bool = False) -> Tuple[bool, str]:
    """The writer makes a CHD of a tiny data + audio disc (in worker processes), the reader reads it back.

    With ``require_native`` it also fails unless the CHD was made in worker processes with a FLAC audio hunk."""
    from . import cdimage, chdwrite, flacenc
    from . import chd as chdlib
    with tempfile.TemporaryDirectory(prefix="romorg-selfcheck-") as tmp:
        folder = Path(tmp)
        t1, pcm = write_tiny_chd(folder / "reference.chd")
        (folder / "t1.bin").write_bytes(t1)
        (folder / "t2.bin").write_bytes(pcm)
        (folder / "disc.cue").write_text('FILE "t1.bin" BINARY\n  TRACK 01 MODE1/2352\n    INDEX 01 00:00:00\n'
                                         'FILE "t2.bin" BINARY\n  TRACK 02 AUDIO\n    INDEX 01 00:00:00\n')
        info = chdwrite.write_chd(folder / "new.chd", cdimage.open_image(folder / "disc.cue"), threads=2,
                                  processes=True)
        with chdlib.Chd(folder / "new.chd") as c:
            got = [chdlib.hash_track(c, t).sha1 for t in c.tracks]
            checks = c.verify()
        if got != [hashlib.sha1(t1).hexdigest(), hashlib.sha1(pcm).hexdigest()] or not checks["overall"]:
            return False, "the writer's CHD does not read back as the tracks it was made from"
        if info["engine"] != "processes":      # the writer quietly compresses in this process when workers fail
            return False, "the writer's worker processes did not run (it compressed in-process instead)"
    flac_hunks = info.get("stored", {}).get("cdfl", 0)
    if require_native and not flac_hunks:
        return False, (f"the writer made a CHD ({info['engine']}, {flac_hunks} cdfl hunks) but a package needs "
                       "FLAC (cdfl) audio")
    extra = [] if flacenc.available() else ["no libFLAC: audio is stored without FLAC"]
    extra += [] if chdwrite.zstd_available() else ["no Zstandard library: the Zstandard preset is not offered"]
    return True, (f"the writer made a CHD ({info['engine']}, {'/'.join(info['codecs'])}, {flac_hunks} cdfl hunks) "
                  "that reads back identically"
                  + ("; " + "; ".join(extra) if extra else ""))


def windows_package_dirs() -> List[Path]:
    """The folders of the Windows package this runs from, or ``[]`` (a source tree, Linux, the AppImage).

    The single-file exe: its bundle (``sys._MEIPASS``) and the exe's folder. The .zip (``<top>\\python``,
    ``<top>\\app\\romorg``, ``<top>\\app\\native``, see ``packaging/build_windows.ps1``): the ``app`` folder."""
    if not sys.platform.startswith("win"):
        return []
    if getattr(sys, "frozen", False):
        dirs = [Path(sys._MEIPASS)] if getattr(sys, "_MEIPASS", None) else []
        return dirs + [Path(sys.executable).resolve().parent]
    app = Path(__file__).resolve().parent.parent
    if app.name.lower() == "app" and (app / "native").is_dir():
        return [app]
    return []


def _shipped(dirs: List[Path], pattern: str) -> List[Path]:
    out: List[Path] = []
    for d in dirs:
        for sub in (d, d / "native"):
            try:
                out += sorted(p for p in sub.glob(pattern) if p.is_file())
            except OSError:
                pass
    return out


def _inside(path: str, dirs: List[Path]) -> bool:
    try:
        p = Path(path).resolve()
    except (OSError, ValueError):
        return False
    return any(p.is_relative_to(d.resolve()) for d in dirs)


def check_windows_package(dirs: List[Path], require_native: bool = False) -> List[Tuple[str, str]]:
    """What a Windows package brings, one ``(status, text)`` per library (the Windows counterpart of the AppImage's
    bundle check): libFLAC loaded from the package and encoding, ``compression.zstd``, libsndfile."""
    from . import flacenc, flacnative, nativeflac, zstdnative
    out: List[Tuple[str, str]] = []
    where = ", ".join(str(d) for d in dirs)
    flac_dlls = _shipped(dirs, "*FLAC*.dll")
    lib = flacnative.library_path()
    if lib and _inside(lib, dirs):
        pcm = _synthetic_pcm(588 * 4)
        try:
            frames = flacenc.encode(pcm, big_endian=False)
            back, _end = flacnative.decode_frames(frames, 0, 588 * 4)
            why = "" if back.tobytes() == pcm else "the frames do not decode to the test block"
        except Exception as exc:  # noqa: BLE001 - reported
            frames, why = b"", f"{type(exc).__name__}: {exc}"
        if why:
            out.append(("FAIL", f"package libFLAC {lib} loads but cannot encode: {why}"))
        else:
            out.append(("OK", f"package libFLAC {lib} encodes and decodes a test block ({len(frames)} bytes)"))
    elif flac_dlls:
        why = (f"libFLAC {lib} from outside the package was loaded" if lib
               else f"it does not load ({flacnative.status()['note']})")
        out.append(("FAIL", f"the package ships {flac_dlls[0]} but {why}"))
    else:
        out.append(("FAIL" if require_native else "WARN",
                    f"no libFLAC in the package ({where}): audio tracks are stored with LZMA (larger files)"))
    st = zstdnative.status()
    if st["native"]:
        out.append(("OK", f"package Zstandard: {st['library']}"))
    else:
        out.append(("FAIL", f"no Zstandard library in the package ({st['note']}): built with a Python before 3.14?"))
    snd = _shipped(dirs, "libsndfile*.dll")
    if not snd:
        out.append(("SKIP", "no libsndfile in this package (optional: libFLAC decodes the audio)"))
    elif nativeflac.available():
        out.append(("OK", f"package libsndfile loads ({snd[0]})"))
    else:
        out.append(("FAIL", f"the package ships {snd[0]} but it does not load"))
    return out


def check_chdman() -> Tuple[str, str]:
    """``("OK" | "WARN" | "SKIP", text)``"""
    from . import bundle, chdtool
    c = chdtool.bundled_chdman()
    if c is None:
        return "SKIP", "no chdman is shipped (the app reads and writes CHDs itself; an installed chdman is optional)"
    ok, why = chdtool._probe_detail(c.argv, c.environ())
    if ok:
        return "OK", f"the bundled chdman starts ({c.argv[0]})"
    return "WARN", f"the bundled chdman cannot start on this system: {why} (the app falls back to other chdman installs)"


def main(argv: List[str] | None = None) -> int:
    from . import bundle
    require_native = "--require-native" in (sys.argv[1:] if argv is None else argv)
    failures = 0
    root = bundle.bundle_root()
    lines: List[Tuple[str, str]] = []
    if root is not None:
        need = [root / "licenses" / "THIRD_PARTY.md"]
        libdir = root / "tools" / "lib"
        missing = [str(p) for p in need if not p.exists()]
        for name in ("libFLAC", "libzstd"):
            if not (libdir.is_dir() and list(libdir.glob(name + ".so*"))):
                missing.append(f"tools/lib/{name}.so*")
        if missing:
            lines.append(("FAIL", "bundle incomplete: missing " + ", ".join(missing)))
        else:
            lines.append(("OK", f"bundle at {root}: tools/lib/libFLAC, tools/lib/libzstd, licenses/THIRD_PARTY.md"))
        lines += check_bundled_libraries(root)
    else:
        package = windows_package_dirs()
        if package:
            lines += check_windows_package(package, require_native)
        else:
            lines.append(("SKIP", "not running from an AppImage or a Windows package (no bundle)"))
    status, text = check_chdman()
    lines.append((status, text))
    try:
        status, text = check_zstd()
    except Exception as exc:  # noqa: BLE001
        status, text = "WARN", f"Zstandard check: {type(exc).__name__}: {exc}"
    lines.append(("FAIL" if require_native and status != "OK" else status, text))
    for fn in (check_flac, check_scheduler, check_writer, check_rvz):
        try:
            ok, text = check_writer(require_native) if fn is check_writer else fn()
        except Exception as exc:  # noqa: BLE001
            ok, text = False, f"{type(exc).__name__}: {exc}"
        # inside the bundle (or a package: --require-native) libFLAC is REQUIRED; elsewhere its absence only means
        # slower decoding (libsndfile or the pure-Python decoder) and audio stored without FLAC
        required = fn in (check_scheduler, check_rvz) or root is not None or require_native
        lines.append(("OK" if ok else ("FAIL" if required else "WARN"), text))
    for status, text in lines:
        print(f"{status:5} {text}")
        failures += status == "FAIL"
    print("SELF-CHECK " + ("FAILED" if failures else "PASSED"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
