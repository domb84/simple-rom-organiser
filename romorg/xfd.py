"""xfdDecrunch in Python: the XFD package's own xfdmaster.library (and its external decrunchers, ``LIBS:xfd/``) run in the 68000
interpreter, to unpack the files an install's script runs xfdDecrunch on - packed programs (PowerPacker, Imploder, CrunchMania,
Rob Northen ... executables) and packed data - byte for byte as on an Amiga, as the WHDLoad slaves check those files.

The XFD package (Georg Hoermann, Dirk Stoecker; Aminet util/pack/xfdmaster.lha) is "freely distributable for noncommercial use"
with all its files together: it is not part of this app; :func:`romorg.whdinstalls.xfd_package` fetches it as it is.

    x = Decruncher(lha_bytes)
    got = x.decrunch(data)                  # (decrunched bytes, packer name), or None when no packer knows the file
"""

from __future__ import annotations

import struct
from typing import Callable, Optional

from .m68k import Cpu, CpuError
from .rawdic import load_hunks, ImagerError

__all__ = ["Decruncher", "XfdError"]


class XfdError(Exception):
    """The XFD package cannot be used, or it failed on a file."""


_EXEC = 0x3000
_DOS = 0x5000
_LIBS = 0x10000                                              # xfdmaster.library and the external decrunchers
_HEAP = 0x200000
_TOP = 0xF00000
_STACK = 0xFF0000
_ERRORS = {1: "out of memory", 3: "the decruncher cannot do that", 6: "it needs a password", 7: "bad hunk structure",
           8: "the packed data is damaged", 9: "a missing resource (a library)", 10: "it needs a key", 11: "it needs a better CPU",
           13: "a DOS error"}
_XFDOBJ_BUFFERINFO = 1
_XFDFF_RECOGEXTERN = 1
_XFDPFF_ADDR = 2
_MEMF_ANY = 0


def _sx32(v: int) -> int:
    v &= 0xFFFFFFFF
    return v - 0x100000000 if v & 0x80000000 else v


class Decruncher:
    def __init__(self, package: bytes, max_steps: int = 300_000_000) -> None:
        from .lha import LhaFile, LhaError
        try:
            arc = LhaFile(package)
            files = {e.name.replace("\\", "/"): arc.read(e) for e in arc.entries if not e.is_dir}
        except (LhaError, ValueError) as exc:
            raise XfdError(f"the XFD package cannot be read: {exc}") from None
        lib = next((v for k, v in files.items() if k.lower().endswith("libs/xfdmaster.library")), None)
        if lib is None:
            raise XfdError("the XFD package has no xfdmaster.library")
        self.slaves = {k.rsplit("/", 1)[1]: v for k, v in files.items()
                       if "/libs/xfd/" in k.lower() and not k.rsplit("/", 1)[1].startswith(".")}
        self.max_steps = max_steps
        self.cpu = cpu = Cpu(1 << 24)
        self._heap = _HEAP
        self._libs = _LIBS
        self.locks: dict[int, tuple[str, list[str]]] = {}
        self._id = 0
        self._ioerr = 0
        self._depth = 0
        self._cur = 0
        self._memo: dict[bytes, Optional[tuple[bytes, str]]] = {}
        for lvo in range(6, 0x400, 6):
            cpu.hook(_EXEC - lvo, self._lvo("exec", lvo))
            cpu.hook(_DOS - lvo, self._lvo("dos", lvo))
        cpu.w32(4, _EXEC)
        cpu.w16(_EXEC + 20, 40)                              # lib_Version: Kickstart 3.1
        cpu.w16(_EXEC + 296, 0)                              # AttnFlags: a 68000
        cpu.w16(_DOS + 20, 40)
        cpu.w32(_EXEC + 276, self._alloc(0x100))             # ThisTask
        cpu.a[7] = _STACK
        self.base = self._open_library(lib)

    # ------------------------------------------------------------------ running
    def _call(self, at: int, a6: int) -> None:
        """A routine of the library's (``a6`` its base); a call from inside one (MakeLibrary's init) keeps the stack it is on."""
        cpu = self.cpu
        cpu.a[6] = a6
        if not self._depth:
            cpu.a[7] = _STACK
        self._depth += 1
        try:
            cpu.call(at, max_steps=self.max_steps)
        except CpuError as exc:
            raise XfdError(f"xfdmaster.library failed: {exc}") from None
        finally:
            self._depth -= 1

    def _lvo(self, lib: str, lvo: int) -> Callable[[Cpu], None]:
        def f(cpu: Cpu) -> None:
            h = getattr(self, f"_{lib}_{lvo}", None)
            if h is None:
                raise XfdError(f"xfdmaster.library calls {lib}.library -{lvo}, which is not here")
            h(cpu)
        return f

    def _alloc(self, size: int, clear: bool = True) -> int:
        a = (self._heap + 15) & ~15
        self._heap = a + max(size, 0) + 16
        if self._heap >= _TOP:
            raise XfdError("out of memory")
        if clear:
            self.cpu.m[a:a + size] = bytes(size)
        return a

    def _load(self, data: bytes) -> list[int]:
        """LoadSeg: an executable's hunks in their segment list; the hunks' addresses."""
        try:
            addrs = load_hunks(data, self.cpu, self._libs, seglist=True)
        except (ImagerError, struct.error, IndexError) as exc:
            raise XfdError(f"not an Amiga program: {exc}") from None
        end = max(addrs) + self.cpu.r32(max(addrs) - 8) + 0x100
        self._libs = (end + 15) & ~15
        if self._libs >= _HEAP:
            raise XfdError("the decrunchers do not fit")
        return addrs

    # ------------------------------------------------------------------ the library
    def _open_library(self, data: bytes) -> int:
        cpu = self.cpu
        addrs = self._load(data)
        seg = (addrs[0] - 4) >> 2
        tag = None
        for a in addrs:                                      # the RomTag: $4AFC and a pointer to itself
            size = cpu.r32(a - 8) - 8
            for o in range(0, max(size - 26, 0), 2):
                if cpu.r16(a + o) == 0x4AFC and cpu.r32(a + o + 2) == a + o:
                    tag = a + o
                    break
            if tag:
                break
        if tag is None:
            raise XfdError("xfdmaster.library has no RomTag")
        init = cpu.r32(tag + 22)
        if cpu.r8(tag + 10) & 0x80:                          # RTF_AUTOINIT: data size, vectors, structure, init routine
            base = self._make_library(cpu.r32(init + 4), cpu.r32(init + 8), cpu.r32(init + 12), cpu.r32(init), seg)
        else:
            cpu.d[0], cpu.a[0] = 0, seg
            self._call(init, _EXEC)
            base = cpu.d[0]
        if not base:
            raise XfdError("xfdmaster.library did not initialise")
        cpu.d[0] = 39
        self._call(base - 6, base)                           # Open
        if not cpu.d[0]:
            raise XfdError("xfdmaster.library does not open")
        return base

    def _make_library(self, vectors: int, structure: int, init: int, size: int, seg: int) -> int:
        cpu = self.cpu
        funcs = []
        if cpu.r16(vectors) == 0xFFFF:                       # relative words
            p = vectors + 2
            while cpu.r16(p) != 0xFFFF:
                funcs.append((vectors + _sx32(cpu.r16(p) | (0xFFFF0000 if cpu.r16(p) & 0x8000 else 0))) & 0xFFFFFFFF)
                p += 2
        else:
            p = vectors
            while cpu.r32(p) != 0xFFFFFFFF:
                funcs.append(cpu.r32(p))
                p += 4
        neg = (len(funcs) * 6 + 3) & ~3
        base = self._alloc(neg + size) + neg
        for i, f in enumerate(funcs):
            cpu.w16(base - 6 * (i + 1), 0x4EF9)
            cpu.w32(base - 6 * (i + 1) + 2, f)
        cpu.w16(base + 16, neg)
        cpu.w16(base + 18, size)
        if structure:
            self._init_struct(structure, base)
        if init:
            cpu.d[0], cpu.a[0] = base, seg
            self._call(init, _EXEC)
            return cpu.d[0]
        return base

    def _init_struct(self, table: int, base: int) -> None:
        """exec InitStruct: the commands of exec/initializers.i."""
        cpu = self.cpu
        p, dest = table, base
        for _ in range(10000):
            cmd = cpu.r8(p)
            p += 1
            if cmd == 0:
                return
            mode, size, count = cmd >> 6, (cmd >> 4) & 3, (cmd & 15) + 1
            if mode == 2:
                dest = base + cpu.r8(p)
                p += 1
            elif mode == 3:
                dest = base + ((cpu.r8(p) << 16) | cpu.r16(p + 1))
                p += 3
            n = (4, 2, 1, 4)[size]
            if n > 1 and p & 1:
                p += 1
            if mode == 1:
                v = cpu.rd(p, n)
                p += n
                for _ in range(count):
                    cpu.wr(dest, n, v)
                    dest += n
            else:
                for _ in range(count):
                    cpu.wr(dest, n, cpu.rd(p, n))
                    p += n
                    dest += n
            if p & 1:
                p += 1

    # ------------------------------------------------------------------ decrunching
    def decrunch(self, data: bytes) -> Optional[tuple[bytes, str]]:
        """``(decrunched, packer)`` as xfdDecrunch saves it, or None when the file is no packed file XFD knows. The last results are
        kept (by the file's SHA-1): the copies of a game mostly have the same packed files."""
        if not data:
            return None
        import hashlib
        key = hashlib.sha1(data).digest()
        if key in self._memo:
            return self._memo[key]
        got = self._decrunch(data)
        self._memo[key] = got
        while len(self._memo) > 64:
            del self._memo[next(iter(self._memo))]
        return got

    def _decrunch(self, data: bytes) -> Optional[tuple[bytes, str]]:
        cpu = self.cpu
        mark = self._heap
        try:
            cpu.d[0] = _XFDOBJ_BUFFERINFO
            self._call(self.base - 114, self.base)           # xfdAllocObject
            bi = cpu.d[0]
            if not bi:
                raise XfdError("xfdAllocObject failed")
            src = self._alloc(len(data) + 16, clear=False)
            cpu.m[src:src + len(data)] = data
            cpu.m[src + len(data):src + len(data) + 16] = bytes(16)
            cpu.w32(bi + 0, src)
            cpu.w32(bi + 4, len(data))
            cpu.w16(bi + 48, _XFDFF_RECOGEXTERN)
            cpu.a[0] = bi
            self._call(self.base - 54, self.base)            # xfdRecogBuffer
            if not cpu.d[0] & 0xFFFF:
                return None
            name = cpu.cstring(cpu.r32(bi + 12)) if cpu.r32(bi + 12) else "?"
            absolute = bool(cpu.r16(bi + 16) & _XFDPFF_ADDR)   # (an absolute-address cruncher: decrunched like any other - xfdDecrunch
                                                             #  saves the plain data unless told LINKSIMPLE / LINKPRO: Twintris)
            cpu.w32(bi + 24, _MEMF_ANY)
            cpu.a[0] = bi
            self._call(self.base - 60, self.base)            # xfdDecrunchBuffer
            if not cpu.d[0] & 0xFFFF:
                err = cpu.r16(bi + 18)
                raise XfdError(f"{name}: {_ERRORS.get(err, f'error {err:#x}')}")
            out, n = cpu.r32(bi + 20), cpu.r32(bi + 32)
            if absolute:
                name += f" (absolute: ${cpu.r32(bi + 36):x})"      # (xfdbi_DecrAddress: where it would be loaded)
            return bytes(cpu.m[out:out + n]), name
        finally:
            self._heap = mark                                # (each file: the memory of the last one again)

    # ------------------------------------------------------------------ exec
    def _exec_198(self, cpu: Cpu) -> None:                   # AllocMem(d0, d1)
        cpu.d[0] = self._alloc(cpu.d[0])                    # (always cleared: MEMF_CLEAR or not, it does no harm)

    _exec_684 = _exec_198                                    # AllocVec

    def _exec_210(self, cpu: Cpu) -> None:                   # FreeMem, FreeVec, Forbid, Permit, CacheClear ...
        pass

    _exec_690 = _exec_132 = _exec_138 = _exec_636 = _exec_642 = _exec_396 = _exec_414 = _exec_210

    def _exec_624(self, cpu: Cpu) -> None:                   # CopyMem(a0, a1, d0)
        n = cpu.d[0]
        cpu.m[cpu.a[1]:cpu.a[1] + n] = cpu.m[cpu.a[0]:cpu.a[0] + n]

    _exec_630 = _exec_624                                    # CopyMemQuick

    def _exec_84(self, cpu: Cpu) -> None:                    # MakeLibrary(a0 vectors, a1 struct, a2 init, d0 size, d1 seglist)
        cpu.d[0] = self._make_library(cpu.a[0], cpu.a[1], cpu.a[2], cpu.d[0], cpu.d[1])

    def _exec_78(self, cpu: Cpu) -> None:                    # InitStruct(a1 table, a2 memory, d0 size)
        cpu.m[cpu.a[2]:cpu.a[2] + cpu.d[0]] = bytes(cpu.d[0])
        self._init_struct(cpu.a[1], cpu.a[2])

    def _exec_426(self, cpu: Cpu) -> None:                   # SumLibrary
        pass

    def _exec_552(self, cpu: Cpu) -> None:                   # OpenLibrary(a1 name, d0 version)
        name = cpu.cstring(cpu.a[1]).lower()
        cpu.d[0] = _DOS if name == "dos.library" else _EXEC if name == "exec.library" else 0

    _exec_408 = _exec_552                                    # OldOpenLibrary

    def _exec_294(self, cpu: Cpu) -> None:                   # FindTask
        cpu.d[0] = cpu.r32(_EXEC + 276)

    def _exec_306(self, cpu: Cpu) -> None:                   # SetSignal
        cpu.d[0] = 0

    # ------------------------------------------------------------------ dos: LIBS:xfd/ and LoadSeg
    def _name(self, at: int) -> str:
        return self.cpu.cstring(at)

    def _slave(self, name: str) -> Optional[bytes]:
        n = name.replace("\\", "/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]
        return next((v for k, v in self.slaves.items() if k.lower() == n.lower()), None)

    def _new_lock(self, path: str, entries: list[str]) -> int:
        self._id += 1
        self.locks[self._id] = (path, entries)
        return self._id << 2

    def _dos_84(self, cpu: Cpu) -> None:                     # Lock(d1 name, d2 mode)
        name = self._name(cpu.d[1]).lower().rstrip("/")
        if name in ("libs:xfd", "libs:xfd/", "xfd"):
            cpu.d[0] = self._new_lock("libs:xfd", sorted(self.slaves))
        elif self._slave(name) is not None and "xfd" in name:
            cpu.d[0] = self._new_lock(name, [])
        else:
            cpu.d[0] = 0
            self._ioerr = 205                                # ERROR_OBJECT_NOT_FOUND

    def _dos_90(self, cpu: Cpu) -> None:                     # UnLock
        self.locks.pop(cpu.d[1] >> 2, None)

    def _fib(self, fib: int, name: str, is_dir: bool, size: int) -> None:
        cpu = self.cpu
        cpu.m[fib:fib + 260] = bytes(260)
        cpu.w32(fib + 4, 2 if is_dir else 0xFFFFFFFD)
        raw = name.encode("latin-1")[:107]
        cpu.m[fib + 8:fib + 8 + len(raw)] = raw
        cpu.w32(fib + 120, 2 if is_dir else 0xFFFFFFFD)
        cpu.w32(fib + 124, size)

    def _dos_102(self, cpu: Cpu) -> None:                    # Examine(d1 lock, d2 fib)
        lk = self.locks.get(cpu.d[1] >> 2)
        if lk is None:
            cpu.d[0] = 0
            return
        path, entries = lk
        is_dir = path == "libs:xfd"
        self._fib(cpu.d[2], "xfd" if is_dir else path.rsplit("/", 1)[-1], is_dir, 0 if is_dir else len(self._slave(path) or b""))
        self.locks[cpu.d[1] >> 2] = (path, list(entries))
        cpu.d[0] = 0xFFFFFFFF

    def _dos_108(self, cpu: Cpu) -> None:                    # ExNext(d1 lock, d2 fib)
        lk = self.locks.get(cpu.d[1] >> 2)
        if not lk or not lk[1]:
            cpu.d[0] = 0
            self._ioerr = 232                                # ERROR_NO_MORE_ENTRIES
            return
        name = lk[1].pop(0)
        self._fib(cpu.d[2], name, False, len(self.slaves[name]))
        cpu.d[0] = 0xFFFFFFFF

    def _dos_126(self, cpu: Cpu) -> None:                    # CurrentDir(d1 lock): the old one
        old = self._cur
        self._cur = cpu.d[1]
        cpu.d[0] = old

    def _dos_132(self, cpu: Cpu) -> None:                    # IoErr
        cpu.d[0] = self._ioerr

    def _dos_150(self, cpu: Cpu) -> None:                    # LoadSeg(d1 name)
        data = self._slave(self._name(cpu.d[1]))
        if data is None:
            cpu.d[0] = 0
            self._ioerr = 205
            return
        try:
            addrs = self._load(data)
        except XfdError:
            cpu.d[0] = 0
            self._ioerr = 121                                # ERROR_FILE_NOT_OBJECT
            return
        cpu.d[0] = (addrs[0] - 4) >> 2

    def _dos_156(self, cpu: Cpu) -> None:                    # UnLoadSeg
        pass

    def _dos_60(self, cpu: Cpu) -> None:                     # Output
        cpu.d[0] = 0

    def _dos_948(self, cpu: Cpu) -> None:                    # PutStr
        cpu.d[0] = 0
