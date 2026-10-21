"""An install's own Amiga programs, run here: a CLI command (an AmigaDOS executable from the install package - "DecrunchCIV",
"PMFC", "WPatch" ...) run in the 68000 interpreter with its arguments, on the files of the install being made.

What it is given is a small AmigaOS: exec (memory, libraries, devices, RawDoFmt), dos.library (files, locks and directories,
the argument parser, printing, variables, paths), utility.library's arithmetic, the requesters of intuition / reqtools answered
"OK", and trackdisk.device reading sectors of the disks. The files are the installer's (``romorg.amigainstall``): the package,
the disks by their labels, the drawer being installed (``RomorgHD:``) - what the program writes goes there, as it would.

    rc, output = run(exe_bytes, 'RomorgHD:Civilization/data', fs, cwd="")
"""

from __future__ import annotations

import struct
from typing import Any, Callable, Optional

from .m68k import Cpu, CpuError
from .rawdic import ImagerError, load_hunks

__all__ = ["run", "AmigaDosError", "read_args", "raw_do_fmt"]


class AmigaDosError(Exception):
    """The program could not be run here (not a program, a call this does not have, a crash)."""


class _Exit(Exception):
    def __init__(self, rc: int) -> None:
        self.rc = rc


_EXEC = 0x2000
_DOS = 0x3000
_LIBS = 0x4000                                               # other libraries: one each 0x800 apart, the base 0x600 in
_CODE = 0x40000
_HEAP = 0x200000
_TOP = 0xE00000
_STACK = 0xFF0000
_DOSTRUE = 0xFFFFFFFF
_MAX_STEPS = 60_000_000                                    # (about a minute here: a tool that runs longer is stuck)

# dos errors (IoErr)
_E_NO_FREE_STORE = 103
_E_REQUIRED_ARG_MISSING = 116
_E_TOO_MANY_ARGS = 118
_E_BAD_NUMBER = 115
_E_OBJECT_IN_USE = 202
_E_OBJECT_EXISTS = 203
_E_DIR_NOT_FOUND = 204
_E_OBJECT_NOT_FOUND = 205
_E_OBJECT_WRONG_TYPE = 212
_E_NO_MORE_ENTRIES = 232
_E_WRITE_PROTECTED = 214
_FAULTS = {103: "not enough memory available", 115: "bad number", 116: "required argument missing",
           118: "wrong number of arguments", 202: "object in use", 203: "object already exists", 204: "directory not found",
           205: "object not found", 212: "object is not of required type", 214: "disk is write-protected",
           232: "no more entries in directory"}
_OTHER_LIBS = ("intuition.library", "graphics.library", "utility.library", "reqtools.library", "asl.library", "icon.library",
               "mathffp.library", "mathtrans.library", "diskfont.library", "layers.library", "workbench.library",
               "expansion.library", "keymap.library", "locale.library", "commodities.library", "gadtools.library")


def _sx32(v: int) -> int:
    v &= 0xFFFFFFFF
    return v - 0x100000000 if v & 0x80000000 else v


def _sx16(v: int) -> int:
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


# --------------------------------------------------------------------------- paths

def _parse(path: str) -> tuple[str, list[str]]:
    if ":" in path:
        v, rest = path.split(":", 1)
        return v + ":", [p for p in rest.split("/") if p]
    return "", [p for p in path.split("/") if p]


def join_path(cur: str, name: str) -> str:
    """``name`` from the current drawer ``cur`` as AmigaDOS takes it: "Vol:x" from that volume's root, ":x" this volume's, "/x"
    from the parent, "a//b" b in a's parent. A path with no volume is in the install package's drawer."""
    prefix, parts = _parse(cur)
    if ":" in name:
        v, name = name.split(":", 1)
        if v:
            prefix = v + ":"
        parts = []
    while name.startswith("/"):
        if parts:
            parts.pop()
        name = name[1:]
    comps = name.split("/") if name else []
    for i, c in enumerate(comps):
        if c:
            parts.append(c)
        elif i < len(comps) - 1:
            if parts:
                parts.pop()
    return prefix + "/".join(parts)


def _last(path: str) -> str:
    prefix, parts = _parse(path)
    return parts[-1] if parts else prefix.rstrip(":")


def _parent(path: str) -> Optional[str]:
    prefix, parts = _parse(path)
    if not parts:
        return None
    return prefix + "/".join(parts[:-1])


# --------------------------------------------------------------------------- the argument parser

def read_args(template: str, line: str) -> Optional[list[Any]]:
    """AmigaDOS ReadArgs: the values of ``template``'s arguments ("FROM/A,TO/K,ALL/S,NUM/N,FILES/M") in ``line`` - a string, True
    (a switch), an int (/N), a list (/M), None (not given) - or None when the line does not fit it (a required one missing ...)."""
    items = _items(line)
    specs = []
    for t in template.split(","):
        names, *mods = t.split("/")
        specs.append(([n.strip().casefold() for n in names.split("=") if n.strip()], {m.strip().upper() for m in mods if m.strip()}))
    out: list[Any] = [None] * len(specs)
    rest: list[str] = []
    i = 0
    while i < len(items):
        word, quoted = items[i]
        hit = None
        if not quoted:
            key, eq, val = word.partition("=")
            for k, (names, mods) in enumerate(specs):
                if key.casefold() in names:
                    hit = k
                    break
            if hit is not None:
                names, mods = specs[hit]
                if "S" in mods or "T" in mods:
                    out[hit] = True
                elif "F" in mods:
                    out[hit] = (val + " " if eq else "") + " ".join(w for w, _q in items[i + 1:])
                    out[hit] = out[hit].strip()
                    i = len(items)
                    continue
                else:
                    if not eq:
                        i += 1
                        if i >= len(items):
                            return None
                        val = items[i][0]
                    if "M" in mods:
                        out[hit] = (out[hit] or []) + [val]
                    else:
                        out[hit] = val
                i += 1
                continue
        rest.append(word)
        i += 1
    for k, (names, mods) in enumerate(specs):               # the plain words: to the arguments without a keyword, in order
        if out[k] is not None or mods & {"K", "S", "T"}:
            continue
        if "F" in mods and rest:
            out[k] = " ".join(rest)
            rest = []
        elif "M" in mods:
            if rest:
                multi = k
                out[k] = []
                later = sum(1 for j in range(k + 1, len(specs)) if out[j] is None and "A" in specs[j][1]
                            and not specs[j][1] & {"K", "S", "T"})
                take = max(0, len(rest) - later)
                out[multi], rest = rest[:take], rest[take:]
        elif rest:
            out[k] = rest.pop(0)
    if rest:
        return None
    for k, (names, mods) in enumerate(specs):
        if out[k] is None and "A" in mods:
            return None
        if "N" in mods and out[k] is not None:
            vals = out[k] if isinstance(out[k], list) else [out[k]]
            try:
                nums = [int(v, 0) if not str(v).lstrip("-").isdigit() else int(v) for v in vals]
            except ValueError:
                return None
            out[k] = nums if isinstance(out[k], list) else nums[0]
    return out


def _items(line: str) -> list[tuple[str, bool]]:
    out, i, n = [], 0, len(line)
    while i < n:
        while i < n and line[i] in " \t\r\n":
            i += 1
        if i >= n:
            break
        if line[i] == '"':
            i += 1
            s = ""
            while i < n and line[i] != '"':
                if line[i] == "*" and i + 1 < n:
                    i += 1
                    s += {"n": "\n", "e": "\x1b", "N": "\n", "E": "\x1b"}.get(line[i], line[i])
                else:
                    s += line[i]
                i += 1
            i += 1
            out.append((s, True))
        else:
            j = i
            while j < n and line[j] not in " \t\r\n":
                if line[j] == '"' and "=" in line[i:j]:          # (KEY="a value")
                    k = line.find('"', j + 1)
                    j = k + 1 if k > 0 else n
                    continue
                j += 1
            out.append((line[i:j].replace('"', ""), False))
            i = j
    return out


# --------------------------------------------------------------------------- RawDoFmt

def raw_do_fmt(fmt: bytes, arg: Callable[[int], int], string: Callable[[int], bytes], bstring: Callable[[int], bytes]) -> bytes:
    """exec's RawDoFmt: ``%[-][0][width][.limit][l]d|u|x|X|c|s|b``, a WORD from the arguments unless "l" (``arg(2)`` / ``arg(4)``
    give the next one)."""
    out = bytearray()
    i, n = 0, len(fmt)
    while i < n:
        c = fmt[i]
        if c != 0x25:
            out.append(c)
            i += 1
            continue
        i += 1
        if i < n and fmt[i] == 0x25:
            out.append(0x25)
            i += 1
            continue
        left = zero = False
        while i < n and fmt[i] in b"-0":
            left |= fmt[i] == 0x2D
            zero |= fmt[i] == 0x30
            i += 1
        width = 0
        while i < n and 0x30 <= fmt[i] <= 0x39:
            width = width * 10 + fmt[i] - 0x30
            i += 1
        limit = None
        if i < n and fmt[i] == 0x2E:
            i += 1
            limit = 0
            while i < n and 0x30 <= fmt[i] <= 0x39:
                limit = limit * 10 + fmt[i] - 0x30
                i += 1
        long = False
        if i < n and fmt[i] in b"lL":
            long = True
            i += 1
        if i >= n:
            break
        t = chr(fmt[i])
        i += 1
        if t in "dDuUxXc":
            v = arg(4 if long else 2)
            if t in "dD":
                s = str(_sx32(v) if long else _sx16(v)).encode()
            elif t in "uU":
                s = str(v & (0xFFFFFFFF if long else 0xFFFF)).encode()
            elif t in "xX":
                s = (f"{v:x}" if t == "x" else f"{v:X}").encode()
            else:
                s = bytes([v & 0xFF])
        elif t in "sb":
            p = arg(4)
            s = string(p) if t == "s" else bstring(p)
            if limit is not None:
                s = s[:limit]
        else:
            s = b""
        if len(s) < width:
            pad = (b"0" if zero and not left else b" ") * (width - len(s))
            s = s + pad if left else pad + s
        out += s
    return bytes(out)


# --------------------------------------------------------------------------- the machine

class _Handle:
    def __init__(self, path: str, data: bytes, writing: bool, console: str = "") -> None:
        self.path = path
        self.data = bytearray(data)
        self.pos = 0
        self.writing = writing
        self.dirty = writing
        self.console = console                              # "out", "in", "nil": not a file
        self.unget: Optional[int] = None


class _Machine:
    def __init__(self, fs: Any, cwd: str, disks: Optional[list[Any]] = None, name: str = "program",
                 shell: Optional[Callable[[str, str], int]] = None, max_steps: int = _MAX_STEPS) -> None:
        self.fs = fs
        self.disks = disks or []
        self.shell = shell
        self.max_steps = max_steps
        self.cpu = cpu = Cpu(1 << 24)
        self.heap = _HEAP
        self.out = bytearray()
        self.key_waiting = True
        self.ioerr = 0
        self.handles: dict[int, _Handle] = {}
        self.locks: dict[int, str] = {}
        self.scans: dict[int, tuple[list[tuple[str, bool]], int]] = {}
        self.vars: dict[str, bytes] = {}
        self.libs: dict[str, int] = {"exec.library": _EXEC, "dos.library": _DOS}
        self.lib_names: dict[int, str] = {_EXEC: "exec", _DOS: "dos"}
        for base, lib in ((_EXEC, "exec"), (_DOS, "dos")):
            self._hook_lib(base, lib)
        for k, nm in enumerate(_OTHER_LIBS):
            base = _LIBS + 0x800 * k + 0x600
            short = nm.split(".")[0]
            self.libs[nm] = base
            self.lib_names[base] = short
            self._hook_lib(base, short)
            cpu.w16(base + 20, 40)
        self.trackdisk = self._alloc(64)
        cpu.w32(4, _EXEC)
        cpu.w16(_EXEC + 20, 40)
        cpu.w16(_EXEC + 296, 0)
        cpu.w16(_DOS + 20, 40)
        self.stdout = self._new_fh(_Handle("*", b"", True, "out"))
        self.stdin = self._new_fh(_Handle("*", b"", False, "in"))
        proc = self._alloc(256)
        cpu.w8(proc + 8, 13)                                  # NT_PROCESS
        cpu.w32(proc + 58, _STACK - 0x10000)                  # tc_SPLower / Upper
        cpu.w32(proc + 62, _STACK)
        cpu.w32(proc + 156, self.stdin)                       # pr_CIS, pr_COS
        cpu.w32(proc + 160, self.stdout)
        cli = self._alloc(64)
        self.cmd_name = self._alloc(256)
        cpu.w8(self.cmd_name, min(len(name), 255))
        cpu.load(self.cmd_name + 1, name.encode("latin-1")[:255])
        cpu.w32(cli + 16, self.cmd_name >> 2)
        cpu.w32(cli + 28, self.stdin)
        cpu.w32(cli + 32, self.stdin)
        cpu.w32(cli + 48, self.stdout)
        cpu.w32(cli + 52, 4096)
        cpu.w32(cli + 56, self.stdout)
        cpu.w32(proc + 172, cli >> 2)                         # pr_CLI: started from a shell
        self.proc, self.cli = proc, cli
        self.cur = self._new_lock(cwd)
        cpu.w32(proc + 152, self.cur)                         # pr_CurrentDir
        cpu.w32(_EXEC + 276, proc)                            # ThisTask
        self.argstr = 0
        self.program = name

    # -- memory, strings
    def _alloc(self, size: int, clear: bool = True) -> int:
        a = (self.heap + 15) & ~15
        self.heap = a + max(size, 0) + 16
        if self.heap >= _TOP:
            raise AmigaDosError("out of memory")
        if clear:
            self.cpu.m[a:a + size] = bytes(size)
        return a

    def _cstr(self, at: int) -> str:
        return self._cbytes(at).decode("latin-1")

    def _cbytes(self, at: int) -> bytes:
        if not at:
            return b""
        m = self.cpu.m
        end = m.find(b"\0", at & 0xFFFFFF, (at & 0xFFFFFF) + 65536)
        return bytes(m[at & 0xFFFFFF:end if end >= 0 else (at & 0xFFFFFF) + 255])

    def _bstr(self, bptr: int) -> bytes:
        a = (bptr << 2) & 0xFFFFFF
        return bytes(self.cpu.m[a + 1:a + 1 + self.cpu.m[a]]) if a else b""

    def _put_cstr(self, s: bytes) -> int:
        a = self._alloc(len(s) + 1)
        self.cpu.load(a, s + b"\0")
        return a

    # -- libraries
    def _hook_lib(self, base: int, lib: str) -> None:
        for lvo in range(6, 0x600, 6):
            self.cpu.hook(base - lvo, self._lvo(lib, lvo))

    def _lvo(self, lib: str, lvo: int) -> Callable[[Cpu], None]:
        def f(cpu: Cpu) -> None:
            h = getattr(self, f"_{lib}_{lvo}", None)
            if h is None:
                raise AmigaDosError(f"{self.program} calls {lib}.library -{lvo}, which is not here")
            h(cpu)
            v = cpu.d[0] & 0xFFFFFFFF                         # (the flags as a library leaves them: its result tested - code
            cpu.n, cpu.z, cpu.v, cpu.c = v >> 31, int(v == 0), 0, 0   #  that branches on them right after the call - kcdepack)
        return f

    # -- files and locks
    def _new_fh(self, h: _Handle) -> int:
        a = self._alloc(48)
        self.handles[a >> 2] = h
        return a >> 2

    def _new_lock(self, path: str) -> int:
        a = self._alloc(24)
        self.locks[a >> 2] = path
        return a >> 2

    def _path(self, name_at: int, base_lock: Optional[int] = None) -> str:
        cur = self.locks.get(self.cpu.r32(self.proc + 152) if base_lock is None else base_lock, "")
        return join_path(cur, self._cstr(name_at))

    def _kind(self, path: str) -> int:
        try:
            return self.fs.exists(path)
        except Exception:  # noqa: BLE001 - a volume that is not there
            return 0

    def _read(self, path: str) -> Optional[bytes]:
        try:
            return self.fs.read(path)
        except Exception:  # noqa: BLE001 - not there, or cannot be read: the program gets an error back
            return None

    def flush(self) -> None:
        for h in self.handles.values():
            if h.dirty and not h.console:
                self.fs.write(h.path, bytes(h.data))
                h.dirty = False

    def _fib(self, fib: int, path: str, kind: int) -> None:
        cpu = self.cpu
        cpu.m[fib:fib + 260] = bytes(260)
        name = _last(path).encode("latin-1")[:106]
        size = 0
        if kind == 1:
            open_h = next((h for h in self.handles.values() if h.path.casefold() == path.casefold() and not h.console), None)
            data = bytes(open_h.data) if open_h is not None else (self._read(path) or b"")
            size = len(data)
        t = 2 if kind == 2 else 0xFFFFFFFD
        cpu.w32(fib + 4, t)
        cpu.w8(fib + 8, len(name))
        cpu.load(fib + 9, name)
        cpu.w32(fib + 120, t)
        cpu.w32(fib + 124, size)
        cpu.w32(fib + 128, (size + 487) // 488)

    # -- exec
    def _exec_198(self, cpu: Cpu) -> None:                   # AllocMem(d0 size, d1 flags)
        cpu.d[0] = self._alloc(cpu.d[0] & 0xFFFFFF, True)

    def _exec_684(self, cpu: Cpu) -> None:                   # AllocVec
        a = self._alloc((cpu.d[0] & 0xFFFFFF) + 8)
        cpu.d[0] = a + 8

    def _nop0(self, cpu: Cpu) -> None:
        cpu.d[0] = 0

    # FreeMem, FreeVec, Forbid, Permit, Disable, Enable, CacheClearU, CacheClearE, CloseLibrary, CloseDevice, FreeSignal,
    # ReplyMsg, DeleteMsgPort, DeleteIORequest, InitSemaphore, ObtainSemaphore, ReleaseSemaphore, FreeEntry, RemPort, AddPort
    _exec_210 = _exec_690 = _exec_132 = _exec_138 = _exec_120 = _exec_126 = _exec_636 = _exec_642 = _exec_414 = _exec_450 = \
        _exec_336 = _exec_378 = _exec_672 = _exec_660 = _exec_558 = _exec_564 = _exec_570 = _exec_228 = _exec_360 = _exec_354 = \
        _exec_678 = _nop0

    # exec lists: lh_Head 0, lh_Tail 4, lh_TailPred 8; a node: ln_Succ 0, ln_Pred 4, ln_Pri 9, ln_Name 10
    def _insert(self, cpu: Cpu, node: int, pred: int) -> None:
        succ = cpu.r32(pred)
        cpu.w32(node, succ)
        cpu.w32(node + 4, pred)
        cpu.w32(succ + 4, node)
        cpu.w32(pred, node)

    def _exec_234(self, cpu: Cpu) -> None:                   # Insert(a0 list, a1 node, a2 pred)
        self._insert(cpu, cpu.a[1], cpu.a[2] or cpu.a[0])

    def _exec_240(self, cpu: Cpu) -> None:                   # AddHead(a0 list, a1 node)
        self._insert(cpu, cpu.a[1], cpu.a[0])

    def _exec_246(self, cpu: Cpu) -> None:                   # AddTail(a0 list, a1 node)
        self._insert(cpu, cpu.a[1], cpu.r32(cpu.a[0] + 8))

    def _unlink(self, cpu: Cpu, node: int) -> None:
        succ, pred = cpu.r32(node), cpu.r32(node + 4)
        cpu.w32(pred, succ)
        cpu.w32(succ + 4, pred)

    def _exec_252(self, cpu: Cpu) -> None:                   # Remove(a1 node)
        self._unlink(cpu, cpu.a[1])

    def _exec_258(self, cpu: Cpu) -> None:                   # RemHead(a0 list)
        n = cpu.r32(cpu.a[0])
        if cpu.r32(n) == 0:
            cpu.d[0] = 0
            return
        self._unlink(cpu, n)
        cpu.d[0] = n

    def _exec_264(self, cpu: Cpu) -> None:                   # RemTail(a0 list)
        n = cpu.r32(cpu.a[0] + 8)
        if cpu.r32(n + 4) == 0:
            cpu.d[0] = 0
            return
        self._unlink(cpu, n)
        cpu.d[0] = n

    def _exec_270(self, cpu: Cpu) -> None:                   # Enqueue(a0 list, a1 node): by priority
        pri = _sx16(cpu.r8(cpu.a[1] + 9) << 8) >> 8
        n = cpu.r32(cpu.a[0])
        while cpu.r32(n) and (_sx16(cpu.r8(n + 9) << 8) >> 8) >= pri:
            n = cpu.r32(n)
        self._insert(cpu, cpu.a[1], cpu.r32(n + 4))

    def _exec_276(self, cpu: Cpu) -> None:                   # FindName(a0 list or node, a1 name)
        want = self._cstr(cpu.a[1])
        n = cpu.r32(cpu.a[0])
        while n and cpu.r32(n):
            if self._cstr(cpu.r32(n + 10)) == want:
                cpu.d[0] = n
                return
            n = cpu.r32(n)
        cpu.d[0] = 0

    def _exec_186(self, cpu: Cpu) -> None:                   # Allocate(a0 header, d0 size): from the heap
        cpu.d[0] = self._alloc(cpu.d[0] & 0xFFFFFF)

    def _exec_192(self, cpu: Cpu) -> None:                   # Deallocate
        cpu.d[0] = 0

    def _exec_222(self, cpu: Cpu) -> None:                   # AllocEntry(a0 memlist): a copy with the memory in it
        src = cpu.a[0]
        n = cpu.r16(src + 14)
        ml = self._alloc(16 + 8 * n)
        cpu.load(ml, bytes(cpu.m[src:src + 16]))
        for i in range(n):
            size = cpu.r32(src + 16 + 8 * i + 4)
            cpu.w32(ml + 16 + 8 * i, self._alloc(size & 0xFFFFFF))
            cpu.w32(ml + 16 + 8 * i + 4, size)
        cpu.d[0] = ml

    def _exec_732(self, cpu: Cpu) -> None:                   # StackSwap(a0 StackSwapStruct: lower, upper, pointer)
        st = cpu.a[0]
        new_sp = cpu.r32(st + 8)
        lower, upper = cpu.r32(st), cpu.r32(st + 4)
        cpu.w32(st, cpu.r32(self.proc + 58))
        cpu.w32(st + 4, cpu.r32(self.proc + 62))
        cpu.w32(st + 8, cpu.a[7])
        cpu.w32(self.proc + 58, lower)
        cpu.w32(self.proc + 62, upper)
        cpu.a[7] = new_sp

    def _exec_216(self, cpu: Cpu) -> None:                   # AvailMem
        cpu.d[0] = 0x400000

    def _exec_294(self, cpu: Cpu) -> None:                   # FindTask
        cpu.d[0] = self.proc

    def _exec_306(self, cpu: Cpu) -> None:                   # SetSignal: none set
        cpu.d[0] = 0

    def _exec_318(self, cpu: Cpu) -> None:                   # Wait: nothing will come
        raise AmigaDosError(f"{self.program} waits for something (a window, a message): not here")

    def _exec_330(self, cpu: Cpu) -> None:                   # AllocSignal
        cpu.d[0] = 16

    def _exec_372(self, cpu: Cpu) -> None:                   # GetMsg: none
        cpu.d[0] = 0

    _exec_384 = _exec_318                                    # WaitPort

    def _exec_552(self, cpu: Cpu) -> None:                   # OpenLibrary(a1 name, d0 version)
        cpu.d[0] = self.libs.get(self._cstr(cpu.a[1]).casefold().rsplit("/", 1)[-1].rsplit(":", 1)[-1], 0)

    _exec_408 = _exec_552                                    # OldOpenLibrary

    def _exec_498(self, cpu: Cpu) -> None:                   # OpenResource
        cpu.d[0] = 0

    def _exec_624(self, cpu: Cpu) -> None:                   # CopyMem(a0, a1, d0)
        n = cpu.d[0] & 0xFFFFFF
        src, dst = cpu.a[0] & 0xFFFFFF, cpu.a[1] & 0xFFFFFF
        cpu.m[dst:dst + n] = cpu.m[src:src + n]

    _exec_630 = _exec_624                                    # CopyMemQuick

    def _exec_666(self, cpu: Cpu) -> None:                   # CreateMsgPort
        cpu.d[0] = self._alloc(40)

    def _exec_654(self, cpu: Cpu) -> None:                   # CreateIORequest(a0 port, d0 size)
        cpu.d[0] = self._alloc(max(cpu.d[0] & 0xFFFF, 48))

    def _exec_444(self, cpu: Cpu) -> None:                   # OpenDevice(a0 name, d0 unit, a1 io, d1 flags)
        name = self._cstr(cpu.a[0]).casefold()
        if name != "trackdisk.device" or (cpu.d[0] & 0xFF) >= max(len(self.disks), 1):
            cpu.d[0] = 0xFFFFFFFF
            cpu.w8(cpu.a[1] + 31, 0xFF)
            return
        cpu.w32(cpu.a[1] + 20, self.trackdisk)
        cpu.w32(cpu.a[1] + 24, cpu.d[0] & 0xFF)               # (io_Unit: the drive)
        cpu.w8(cpu.a[1] + 31, 0)
        cpu.d[0] = 0

    def _exec_456(self, cpu: Cpu) -> None:                   # DoIO(a1 io): trackdisk.device
        io = cpu.a[1]
        cmd = cpu.r16(io + 28) & 0x7FFF
        unit = cpu.r32(io + 24)
        length, data, offset = cpu.r32(io + 36), cpu.r32(io + 40), cpu.r32(io + 44)
        cpu.w8(io + 31, 0)
        cpu.w32(io + 32, 0)
        disk = self._disk(unit)
        if cmd == 2:                                          # CMD_READ
            img = self._image(disk)
            if img is None:
                cpu.w8(io + 31, 21)                           # (TDERR_NoSecHdr: not an AmigaDOS-format track)
            else:
                chunk = img[offset:offset + length]
                cpu.load(data, chunk + bytes(length - len(chunk)))
                cpu.w32(io + 32, length)
        elif cmd == 16:                                       # TD_RAWREAD: the track's MFM from the index (or a sync word)
            raw = self._rawread(disk, offset, length, cpu.r8(io + 30))
            if raw is None:
                cpu.w8(io + 31, 21)
            else:
                cpu.load(data, raw)
                cpu.w32(io + 32, length)
        elif cmd in (3, 11):                                  # CMD_WRITE, TD_FORMAT: the disks stay as they are
            cpu.w8(io + 31, 28)                               # (TDERR_WriteProt)
        elif cmd in (13, 14, 15, 18, 19):                     # TD_CHANGENUM, CHANGESTATE, PROTSTATUS, GETDRIVETYPE, GETNUMTRACKS
            cpu.w32(io + 32, {13: 1, 14: 0 if disk is not None else 1, 15: 1, 18: 1, 19: 160}[cmd])
        cpu.d[0] = cpu.r8(io + 31)

    _exec_462 = _exec_456                                    # SendIO (done at once)

    def _exec_474(self, cpu: Cpu) -> None:                   # WaitIO
        cpu.d[0] = cpu.r8(cpu.a[1] + 31)

    _exec_468 = _exec_474                                    # CheckIO

    def _exec_480(self, cpu: Cpu) -> None:                   # AbortIO
        cpu.d[0] = 0

    def _exec_522(self, cpu: Cpu) -> None:                   # RawDoFmt(a0 fmt, a1 data, a2 putch, a3 putchdata)
        args = [cpu.a[1]]
        def arg(n: int) -> int:
            v = cpu.r32(args[0]) if n == 4 else cpu.r16(args[0])
            args[0] += n
            return v
        text = raw_do_fmt(self._cbytes(cpu.a[0]), arg, self._cbytes, self._bstr) + b"\0"
        put, data = cpu.a[2], cpu.a[3]
        if bytes(cpu.m[put:put + 4]) == b"\x16\xc0\x4e\x75":   # (move.b d0,(a3)+ ; rts: into a buffer)
            cpu.load(data, text)
            cpu.a[3] = data + len(text)
        elif put:
            saved = (list(cpu.d), list(cpu.a))
            a3 = data
            for ch in text:
                cpu.d[0] = ch
                cpu.a[3] = a3
                try:
                    cpu.call(put, max_steps=1_000_000)
                except CpuError as exc:
                    raise AmigaDosError(f"{self.program}: RawDoFmt's routine failed ({exc})") from None
                a3 = cpu.a[3]
            cpu.d[:], cpu.a[:] = saved
            cpu.a[3] = a3
        cpu.d[0] = args[0]

    def _disk(self, unit: int) -> Any:
        if not self.disks:
            return None
        cur = getattr(self.fs, "current", None)
        if unit == 0 and cur is not None:
            d = getattr(self.fs, "disks", {}).get(cur)
            if d is not None:
                return d
        return self.disks[unit] if unit < len(self.disks) else None

    @staticmethod
    def _rawread(disk: Any, track: int, length: int, flags: int) -> Optional[bytes]:
        """``length`` bytes of track ``track``'s MFM as the drive reads it: from the index (IOTDF_INDEXSYNC), from a $4489 sync word
        (IOTDF_WORDSYNC), the turn read again and again."""
        tracks = getattr(disk, "tracks", None)
        got = tracks.track(track) if tracks is not None else None
        if not got or not got[1]:
            return None
        turn, nbits = got
        bits = int.from_bytes(turn, "big") >> (8 * len(turn) - nbits)
        start = 0
        if flags & 0x20:                                      # (WORDSYNC: from the first sync word, round the turn)
            twice = (bits << nbits) | bits
            start = next((i for i in range(nbits) if (twice >> (2 * nbits - 16 - i)) & 0xFFFF == 0x4489), None)
            if start is None:
                return None
        want = 8 * length
        out, have = 0, 0
        first = bits & ((1 << (nbits - start)) - 1)
        out, have = first, nbits - start
        while have < want:
            out = (out << nbits) | bits
            have += nbits
        return (out >> (have - want)).to_bytes(length, "big")

    @staticmethod
    def _image(disk: Any) -> Optional[bytes]:
        for attr in ("data", "image", "raw"):
            v = getattr(disk, attr, None)
            if isinstance(v, (bytes, bytearray)) and len(v) >= 901120:
                return bytes(v)
        return None

    # -- dos: files
    def _dos_30(self, cpu: Cpu) -> None:                     # Open(d1 name, d2 mode)
        name = self._cstr(cpu.d[1])
        mode = cpu.d[2]
        low = name.casefold()
        if low in ("*", "console:") or low.startswith(("con:", "raw:")):
            cpu.d[0] = self._new_fh(_Handle("*", b"", True, "out"))
            return
        if low.startswith("nil:"):
            cpu.d[0] = self._new_fh(_Handle("NIL:", b"", True, "nil"))
            return
        path = self._path(cpu.d[1])
        kind = self._kind(path)
        if mode == 1006:                                      # MODE_NEWFILE
            if kind == 2:
                self.ioerr, cpu.d[0] = _E_OBJECT_WRONG_TYPE, 0
                return
            cpu.d[0] = self._new_fh(_Handle(path, b"", True))
            return
        if kind == 2:
            self.ioerr, cpu.d[0] = _E_OBJECT_WRONG_TYPE, 0
            return
        data = self._read(path) if kind == 1 else None
        if data is None:
            if mode == 1004:                                  # MODE_READWRITE: made when it is not there
                cpu.d[0] = self._new_fh(_Handle(path, b"", True))
                return
            self.ioerr, cpu.d[0] = _E_OBJECT_NOT_FOUND, 0
            return
        cpu.d[0] = self._new_fh(_Handle(path, data, False))

    def _dos_36(self, cpu: Cpu) -> None:                     # Close(d1 fh)
        h = self.handles.pop(cpu.d[1], None)
        if h is not None and h.dirty and not h.console:
            self.fs.write(h.path, bytes(h.data))
        cpu.d[0] = _DOSTRUE

    def _dos_42(self, cpu: Cpu) -> None:                     # Read(d1 fh, d2 buf, d3 len)
        h = self.handles.get(cpu.d[1])
        if h is None:
            cpu.d[0] = 0xFFFFFFFF
            return
        n = _sx32(cpu.d[3])
        if h.console == "in" and n > 0:                       # (the console: Return pressed - "press any key", a prompt)
            cpu.load(cpu.d[2], b"\n")
            cpu.d[0] = 1
            self.key_waiting = False
            return
        if h.console or n <= 0:
            cpu.d[0] = 0
            return
        chunk = bytes(h.data[h.pos:h.pos + n])
        cpu.load(cpu.d[2], chunk)
        h.pos += len(chunk)
        cpu.d[0] = len(chunk)

    def _write(self, h: _Handle, data: bytes) -> None:
        if h.console == "out":
            self.out += data
        elif not h.console:
            end = h.pos + len(data)
            if end > len(h.data):
                h.data += bytes(end - len(h.data))
            h.data[h.pos:end] = data
            h.pos = end
            h.dirty = True

    def _dos_48(self, cpu: Cpu) -> None:                     # Write(d1 fh, d2 buf, d3 len)
        h = self.handles.get(cpu.d[1])
        n = _sx32(cpu.d[3])
        if h is None or n < 0:
            cpu.d[0] = 0xFFFFFFFF
            return
        self._write(h, bytes(cpu.m[cpu.d[2] & 0xFFFFFF:(cpu.d[2] & 0xFFFFFF) + n]))
        cpu.d[0] = n

    def _dos_54(self, cpu: Cpu) -> None:                     # Input
        cpu.d[0] = self.stdin

    def _dos_60(self, cpu: Cpu) -> None:                     # Output
        cpu.d[0] = self.stdout

    def _dos_66(self, cpu: Cpu) -> None:                     # Seek(d1 fh, d2 pos, d3 mode)
        h = self.handles.get(cpu.d[1])
        if h is None or h.console:
            cpu.d[0] = 0xFFFFFFFF
            return
        old = h.pos
        base = {0xFFFFFFFF: 0, 0: h.pos, 1: len(h.data)}.get(cpu.d[3], 0)
        new = base + _sx32(cpu.d[2])
        if new < 0 or new > len(h.data):
            self.ioerr, cpu.d[0] = 219, 0xFFFFFFFF            # (ERROR_SEEK_ERROR)
            return
        h.pos = new
        cpu.d[0] = old

    def _dos_72(self, cpu: Cpu) -> None:                     # DeleteFile(d1 name)
        path = self._path(cpu.d[1])
        if not self._kind(path):
            self.ioerr, cpu.d[0] = _E_OBJECT_NOT_FOUND, 0
            return
        self.fs.delete(path)
        cpu.d[0] = _DOSTRUE

    def _dos_78(self, cpu: Cpu) -> None:                     # Rename(d1 old, d2 new)
        old, new = self._path(cpu.d[1]), self._path(cpu.d[2])
        if self._kind(old) != 1:
            self.ioerr, cpu.d[0] = _E_OBJECT_NOT_FOUND, 0
            return
        self.fs.rename(old, new)
        cpu.d[0] = _DOSTRUE

    # -- dos: locks, directories
    def _dos_84(self, cpu: Cpu) -> None:                     # Lock(d1 name, d2 mode)
        path = self._path(cpu.d[1])
        if not self._kind(path):
            self.ioerr, cpu.d[0] = _E_OBJECT_NOT_FOUND, 0
            return
        cpu.d[0] = self._new_lock(path)

    def _dos_90(self, cpu: Cpu) -> None:                     # UnLock
        if cpu.d[1] != self.cur:
            self.locks.pop(cpu.d[1], None)
        cpu.d[0] = 0

    def _dos_96(self, cpu: Cpu) -> None:                     # DupLock
        cpu.d[0] = self._new_lock(self.locks.get(cpu.d[1], "")) if cpu.d[1] in self.locks or not cpu.d[1] else 0

    def _dos_102(self, cpu: Cpu) -> None:                    # Examine(d1 lock, d2 fib)
        path = self.locks.get(cpu.d[1], "") if cpu.d[1] else self.locks.get(self.cur, "")
        kind = self._kind(path) or 2
        self._fib(cpu.d[2], path, kind)
        self.scans.pop(cpu.d[2], None)
        cpu.d[0] = _DOSTRUE

    def _dos_108(self, cpu: Cpu) -> None:                    # ExNext(d1 lock, d2 fib)
        path = self.locks.get(cpu.d[1], "")
        fib = cpu.d[2]
        if fib not in self.scans:
            try:
                entries = list(self.fs.listing(path))
            except Exception:  # noqa: BLE001
                entries = []
            self.scans[fib] = (entries, 0)
        entries, i = self.scans[fib]
        if i >= len(entries):
            self.scans.pop(fib, None)
            self.ioerr, cpu.d[0] = _E_NO_MORE_ENTRIES, 0
            return
        name, is_dir = entries[i]
        self.scans[fib] = (entries, i + 1)
        self._fib(fib, join_path(path, name), 2 if is_dir else 1)
        cpu.d[0] = _DOSTRUE

    def _dos_114(self, cpu: Cpu) -> None:                    # Info(d1 lock, d2 infodata)
        d = cpu.d[2]
        cpu.m[d:d + 36] = bytes(36)
        cpu.w32(d + 8, 82)                                     # (ID_VALIDATED)
        cpu.w32(d + 12, 1760 * 20)
        cpu.w32(d + 16, 1760)
        cpu.w32(d + 20, 512)
        cpu.w32(d + 24, 0x444F5301)
        cpu.d[0] = _DOSTRUE

    def _dos_120(self, cpu: Cpu) -> None:                    # CreateDir(d1 name)
        path = self._path(cpu.d[1])
        if self._kind(path):
            self.ioerr, cpu.d[0] = _E_OBJECT_EXISTS, 0
            return
        self.fs.mkdir(path)
        cpu.d[0] = self._new_lock(path)

    def _dos_126(self, cpu: Cpu) -> None:                    # CurrentDir(d1 lock): the old one
        cpu.d[0] = cpu.r32(self.proc + 152)
        cpu.w32(self.proc + 152, cpu.d[1] if cpu.d[1] in self.locks else self.cur)

    def _dos_132(self, cpu: Cpu) -> None:                    # IoErr
        cpu.d[0] = self.ioerr

    def _dos_144(self, cpu: Cpu) -> None:                    # Exit(d1 rc)
        raise _Exit(_sx32(cpu.d[1]))

    def _dos_150(self, cpu: Cpu) -> None:                    # LoadSeg(d1 name)
        data = self._read(self._path(cpu.d[1]))
        if not data:
            self.ioerr, cpu.d[0] = _E_OBJECT_NOT_FOUND, 0
            return
        at = self._alloc(len(data) * 2 + 0x1000, False)
        try:
            addrs = load_hunks(data, cpu, at, seglist=True)
        except (ImagerError, struct.error, IndexError):
            self.ioerr, cpu.d[0] = 121, 0                      # (ERROR_FILE_NOT_OBJECT)
            return
        self.heap = max(self.heap, max(addrs) + cpu.r32(max(addrs) - 8) + 0x200)
        cpu.d[0] = (addrs[0] - 4) >> 2

    def _dos_156(self, cpu: Cpu) -> None:                    # UnLoadSeg
        cpu.d[0] = _DOSTRUE

    def _dos_174(self, cpu: Cpu) -> None:                    # DeviceProc
        cpu.d[0] = self.proc + 92

    def _true(self, cpu: Cpu) -> None:
        cpu.d[0] = _DOSTRUE

    # SetComment, SetProtection, SetFileDate, Flush, SetOwner, SetVar, DeleteVar, AssignLock, AssignLate, AssignPath, AssignAdd,
    # SetProgramName, SetCurrentDirName, SetPrompt, Relabel, Inhibit
    _dos_180 = _dos_186 = _dos_396 = _dos_360 = _dos_996 = _dos_912 = _dos_612 = _dos_618 = _dos_624 = _dos_630 = _dos_570 = \
        _dos_558 = _dos_582 = _dos_720 = _dos_726 = _true

    def _dos_192(self, cpu: Cpu) -> None:                    # DateStamp(d1 ds): 1 January 1994
        cpu.w32(cpu.d[1], 5844)
        cpu.w32(cpu.d[1] + 4, 720)
        cpu.w32(cpu.d[1] + 8, 0)
        cpu.d[0] = cpu.d[1]

    def _dos_198(self, cpu: Cpu) -> None:                    # Delay
        cpu.d[0] = 0

    def _dos_204(self, cpu: Cpu) -> None:                    # WaitForChar: a key until one is read (a loop that empties the
        cpu.d[0] = _DOSTRUE if self.key_waiting else 0        #  keyboard ends; a read after it still gets Return)

    def _dos_426(self, cpu: Cpu) -> None:                    # SetMode (the console raw or not)
        cpu.d[0] = _DOSTRUE

    def _dos_210(self, cpu: Cpu) -> None:                    # ParentDir(d1 lock)
        p = _parent(self.locks.get(cpu.d[1], ""))
        cpu.d[0] = self._new_lock(p) if p is not None else 0

    def _dos_216(self, cpu: Cpu) -> None:                    # IsInteractive
        h = self.handles.get(cpu.d[1])
        cpu.d[0] = _DOSTRUE if h is not None and h.console in ("out", "in") else 0

    def _dos_222(self, cpu: Cpu) -> None:                    # Execute(d1 command, d2 in, d3 out)
        self._run_command(self._cstr(cpu.d[1]))
        cpu.d[0] = _DOSTRUE

    def _dos_606(self, cpu: Cpu) -> None:                    # SystemTagList(d1 command, d2 tags): its return code
        cpu.d[0] = self._run_command(self._cstr(cpu.d[1])) & 0xFFFFFFFF

    def _run_command(self, line: str) -> int:
        if self.shell is None:
            raise AmigaDosError(f"{self.program} runs \"{line[:60]}\": not here")
        self.flush()
        return self.shell(line, self.locks.get(self.cpu.r32(self.proc + 152), ""))

    def _dos_228(self, cpu: Cpu) -> None:                    # AllocDosObject(d1 type, d2 tags)
        size = {0: 48, 1: 128, 2: 260, 3: 32, 4: 64, 5: 32}.get(cpu.d[1], 64)
        a = self._alloc(size)
        if cpu.d[1] == 0:
            self.handles[a >> 2] = _Handle("NIL:", b"", True, "nil")
        cpu.d[0] = a

    def _dos_234(self, cpu: Cpu) -> None:                    # FreeDosObject
        cpu.d[0] = 0

    def _dos_306(self, cpu: Cpu) -> None:                    # FGetC(d1 fh)
        h = self.handles.get(cpu.d[1])
        if h is None or h.console:
            cpu.d[0] = 0xFFFFFFFF
            return
        if h.unget is not None:
            cpu.d[0], h.unget = h.unget, None
            return
        if h.pos >= len(h.data):
            cpu.d[0] = 0xFFFFFFFF
            return
        cpu.d[0] = h.data[h.pos]
        h.pos += 1

    def _dos_312(self, cpu: Cpu) -> None:                    # FPutC(d1 fh, d2 char)
        h = self.handles.get(cpu.d[1])
        if h is not None:
            self._write(h, bytes([cpu.d[2] & 0xFF]))
        cpu.d[0] = cpu.d[2] & 0xFF

    def _dos_318(self, cpu: Cpu) -> None:                    # UnGetC(d1 fh, d2 char)
        h = self.handles.get(cpu.d[1])
        if h is not None and not h.console:
            if _sx32(cpu.d[2]) == -1 and h.pos:
                h.pos -= 1
            else:
                h.unget = cpu.d[2] & 0xFF
        cpu.d[0] = _DOSTRUE

    def _dos_324(self, cpu: Cpu) -> None:                    # FRead(d1 fh, d2 buf, d3 blocklen, d4 blocks)
        blk, n = cpu.d[3], cpu.d[4]
        cpu.d[3] = blk * n
        self._dos_42(cpu)
        cpu.d[0] = (cpu.d[0] // blk) if blk and _sx32(cpu.d[0]) >= 0 else 0

    def _dos_330(self, cpu: Cpu) -> None:                    # FWrite
        blk, n = cpu.d[3], cpu.d[4]
        cpu.d[3] = blk * n
        self._dos_48(cpu)
        cpu.d[0] = n

    def _dos_336(self, cpu: Cpu) -> None:                    # FGets(d1 fh, d2 buf, d3 len)
        h = self.handles.get(cpu.d[1])
        if h is None or h.console or h.pos >= len(h.data):
            cpu.d[0] = 0
            return
        end = h.data.find(b"\n", h.pos, h.pos + max(cpu.d[3] - 1, 0))
        end = (end + 1) if end >= 0 else min(len(h.data), h.pos + max(cpu.d[3] - 1, 0))
        cpu.load(cpu.d[2], bytes(h.data[h.pos:end]) + b"\0")
        h.pos = end
        cpu.d[0] = cpu.d[2]

    def _dos_342(self, cpu: Cpu) -> None:                    # FPuts(d1 fh, d2 string)
        h = self.handles.get(cpu.d[1])
        if h is not None:
            self._write(h, self._cbytes(cpu.d[2]))
        cpu.d[0] = 0

    def _fmt(self, fmt: int, argv: int) -> bytes:
        cpu = self.cpu
        args = [argv]
        def arg(n: int) -> int:
            v = cpu.r32(args[0]) if n == 4 else cpu.r16(args[0])
            args[0] += n
            return v
        return raw_do_fmt(self._cbytes(fmt), arg, self._cbytes, self._bstr)

    def _dos_354(self, cpu: Cpu) -> None:                    # VFPrintf(d1 fh, d2 fmt, d3 argv)
        text = self._fmt(cpu.d[2], cpu.d[3])
        h = self.handles.get(cpu.d[1])
        if h is not None:
            self._write(h, text)
        cpu.d[0] = len(text)

    def _dos_366(self, cpu: Cpu) -> None:                    # SetVBuf
        cpu.d[0] = 0

    def _dos_372(self, cpu: Cpu) -> None:                    # DupLockFromFH
        h = self.handles.get(cpu.d[1])
        cpu.d[0] = self._new_lock(h.path) if h is not None and not h.console else 0

    def _dos_378(self, cpu: Cpu) -> None:                    # OpenFromLock(d1 lock)
        path = self.locks.get(cpu.d[1])
        data = self._read(path) if path is not None else None
        cpu.d[0] = self._new_fh(_Handle(path, data, False)) if data is not None else 0

    def _dos_384(self, cpu: Cpu) -> None:                    # ParentOfFH
        h = self.handles.get(cpu.d[1])
        p = _parent(h.path) if h is not None else None
        cpu.d[0] = self._new_lock(p) if p is not None else 0

    def _dos_390(self, cpu: Cpu) -> None:                    # ExamineFH(d1 fh, d2 fib)
        h = self.handles.get(cpu.d[1])
        if h is None or h.console:
            cpu.d[0] = 0
            return
        self._fib(cpu.d[2], h.path, 1)
        cpu.w32(cpu.d[2] + 124, len(h.data))
        cpu.d[0] = _DOSTRUE

    def _dos_402(self, cpu: Cpu) -> None:                    # NameFromLock(d1 lock, d2 buf, d3 len)
        p = self.locks.get(cpu.d[1], "")
        name = (p if ":" in p else "PROGDIR:" + p).encode("latin-1")
        if len(name) + 1 > cpu.d[3]:
            self.ioerr, cpu.d[0] = 120, 0                      # (ERROR_LINE_TOO_LONG)
            return
        cpu.load(cpu.d[2], name + b"\0")
        cpu.d[0] = _DOSTRUE

    def _dos_408(self, cpu: Cpu) -> None:                    # NameFromFH
        h = self.handles.get(cpu.d[1])
        lock = self._new_lock(h.path if h is not None else "")
        cpu.d[1] = lock
        self._dos_402(cpu)

    def _dos_420(self, cpu: Cpu) -> None:                    # SameLock
        a, b = self.locks.get(cpu.d[1]), self.locks.get(cpu.d[2])
        cpu.d[0] = 0 if a is not None and b is not None and a.casefold() == b.casefold() else 0xFFFFFFFF

    def _dos_456(self, cpu: Cpu) -> None:                    # SetFileSize(d1 fh, d2 pos, d3 mode)
        h = self.handles.get(cpu.d[1])
        if h is None or h.console:
            cpu.d[0] = 0xFFFFFFFF
            return
        base = {0xFFFFFFFF: 0, 0: h.pos, 1: len(h.data)}.get(cpu.d[3], 0)
        size = max(0, base + _sx32(cpu.d[2]))
        h.data = h.data[:size] + bytes(max(0, size - len(h.data)))
        h.pos = min(h.pos, size)
        h.dirty = True
        cpu.d[0] = size

    def _dos_462(self, cpu: Cpu) -> None:                    # SetIoErr
        cpu.d[0], self.ioerr = self.ioerr, cpu.d[1]

    def _fault(self, code: int, header: bytes) -> bytes:
        text = _FAULTS.get(code, f"error {code}").encode()
        return (header + b": " + text) if header else text

    def _dos_468(self, cpu: Cpu) -> None:                    # Fault(d1 code, d2 header, d3 buf, d4 len)
        text = self._fault(_sx32(cpu.d[1]), self._cbytes(cpu.d[2]))[:max(cpu.d[4] - 1, 0)]
        cpu.load(cpu.d[3], text + b"\0")
        cpu.d[0] = len(text)

    def _dos_474(self, cpu: Cpu) -> None:                    # PrintFault(d1 code, d2 header)
        self.out += self._fault(_sx32(cpu.d[1]), self._cbytes(cpu.d[2])) + b"\n"
        cpu.d[0] = _DOSTRUE

    def _dos_492(self, cpu: Cpu) -> None:                    # Cli
        cpu.d[0] = self.cli

    def _dos_534(self, cpu: Cpu) -> None:                    # GetArgStr
        cpu.d[0] = self.argstr

    def _dos_540(self, cpu: Cpu) -> None:                    # SetArgStr
        cpu.d[0], self.argstr = self.argstr, cpu.d[1]

    def _dos_564(self, cpu: Cpu) -> None:                    # GetCurrentDirName(d1 buf, d2 len)
        cpu.d[3], cpu.d[2], cpu.d[1] = cpu.d[2], cpu.d[1], cpu.r32(self.proc + 152)
        self._dos_402(cpu)

    def _dos_576(self, cpu: Cpu) -> None:                    # GetProgramName(d1 buf, d2 len)
        name = self.program.encode("latin-1")[:max(cpu.d[2] - 1, 0)]
        cpu.load(cpu.d[1], name + b"\0")
        cpu.d[0] = _DOSTRUE

    def _dos_588(self, cpu: Cpu) -> None:                    # GetPrompt
        cpu.load(cpu.d[1], b"> \0")
        cpu.d[0] = _DOSTRUE

    def _dos_600(self, cpu: Cpu) -> None:                    # GetProgramDir: the current drawer (the package's)
        cpu.d[0] = self.cur

    def _dos_708(self, cpu: Cpu) -> None:                    # IsFileSystem
        cpu.d[0] = _DOSTRUE

    def _dos_792(self, cpu: Cpu) -> None:                    # CheckSignal: no Ctrl-C
        cpu.d[0] = 0

    def _dos_798(self, cpu: Cpu) -> None:                    # ReadArgs(d1 template, d2 array, d3 rdargs)
        template = self._cstr(cpu.d[1])
        line = self._cstr(self.argstr)
        rda = cpu.d[3]
        if rda and cpu.r32(rda):                              # (RDArgs with its own source: CS_Buffer, CS_Length)
            buf, n = cpu.r32(rda), cpu.r32(rda + 4)
            line = bytes(cpu.m[buf:buf + n]).decode("latin-1")
        if line.strip() == "?":
            line = ""
        vals = read_args(template, line)
        if vals is None:
            self.ioerr, cpu.d[0] = _E_REQUIRED_ARG_MISSING, 0
            return
        arr = cpu.d[2]
        for k, v in enumerate(vals):
            if v is None:
                continue
            if v is True:
                cpu.w32(arr + 4 * k, _DOSTRUE)
            elif isinstance(v, int):
                p = self._alloc(4)
                cpu.w32(p, v & 0xFFFFFFFF)
                cpu.w32(arr + 4 * k, p)
            elif isinstance(v, list):
                p = self._alloc(4 * (len(v) + 1))
                for j, x in enumerate(v):
                    if isinstance(x, int):
                        q = self._alloc(4)
                        cpu.w32(q, x & 0xFFFFFFFF)
                    else:
                        q = self._put_cstr(str(x).encode("latin-1"))
                    cpu.w32(p + 4 * j, q)
                cpu.w32(arr + 4 * k, p)
            else:
                cpu.w32(arr + 4 * k, self._put_cstr(str(v).encode("latin-1")))
        cpu.d[0] = rda or self._alloc(32)

    def _dos_804(self, cpu: Cpu) -> None:                    # FindArg(d1 template, d2 keyword)
        key = self._cstr(cpu.d[2]).casefold()
        for k, t in enumerate(self._cstr(cpu.d[1]).split(",")):
            if key in [n.strip().casefold() for n in t.split("/")[0].split("=")]:
                cpu.d[0] = k
                return
        cpu.d[0] = 0xFFFFFFFF

    def _dos_816(self, cpu: Cpu) -> None:                    # StrToLong(d1 string, d2 &value): characters used, or -1
        s = self._cstr(cpu.d[1])
        i = 0
        while i < len(s) and s[i] in " \t":
            i += 1
        j = i + (1 if i < len(s) and s[i] in "+-" else 0)
        k = j
        while k < len(s) and s[k].isdigit():
            k += 1
        if k == j:
            cpu.d[0] = 0xFFFFFFFF
            return
        cpu.w32(cpu.d[2], int(s[i:k]) & 0xFFFFFFFF)
        cpu.d[0] = k

    def _dos_858(self, cpu: Cpu) -> None:                    # FreeArgs
        cpu.d[0] = 0

    def _dos_870(self, cpu: Cpu) -> None:                    # FilePart(d1 path)
        s = self._cbytes(cpu.d[1])
        cut = max(s.rfind(b"/"), s.rfind(b":"))
        cpu.d[0] = cpu.d[1] + cut + 1

    def _dos_876(self, cpu: Cpu) -> None:                    # PathPart(d1 path)
        s = self._cbytes(cpu.d[1])
        sl = s.rfind(b"/")
        cpu.d[0] = cpu.d[1] + (sl if sl >= 0 else s.rfind(b":") + 1)

    def _dos_882(self, cpu: Cpu) -> None:                    # AddPart(d1 dir buffer, d2 file, d3 size)
        new = join_path(self._cstr(cpu.d[1]), self._cstr(cpu.d[2])).encode("latin-1")
        if not self._cstr(cpu.d[1]):
            new = self._cbytes(cpu.d[2])
        if len(new) + 1 > cpu.d[3]:
            self.ioerr, cpu.d[0] = 120, 0
            return
        cpu.load(cpu.d[1], new + b"\0")
        cpu.d[0] = _DOSTRUE

    def _dos_900(self, cpu: Cpu) -> None:                    # SetVar(d1 name, d2 buf, d3 size, d4 flags)
        n = _sx32(cpu.d[3])
        val = self._cbytes(cpu.d[2]) if n < 0 else bytes(cpu.m[cpu.d[2]:cpu.d[2] + n])
        self.vars[self._cstr(cpu.d[1]).casefold()] = val
        cpu.d[0] = _DOSTRUE

    def _dos_906(self, cpu: Cpu) -> None:                    # GetVar(d1 name, d2 buf, d3 size, d4 flags)
        v = self.vars.get(self._cstr(cpu.d[1]).casefold())
        if v is None:
            self.ioerr, cpu.d[0] = _E_OBJECT_NOT_FOUND, 0xFFFFFFFF
            return
        v = v[:max(cpu.d[3] - 1, 0)]
        cpu.load(cpu.d[2], v + b"\0")
        cpu.d[0] = len(v)

    def _dos_942(self, cpu: Cpu) -> None:                    # WriteChars(d1 buf, d2 len)
        self.out += bytes(cpu.m[cpu.d[1]:cpu.d[1] + cpu.d[2]])
        cpu.d[0] = cpu.d[2]

    def _dos_948(self, cpu: Cpu) -> None:                    # PutStr(d1 string)
        self.out += self._cbytes(cpu.d[1])
        cpu.d[0] = 0

    def _dos_954(self, cpu: Cpu) -> None:                    # VPrintf(d1 fmt, d2 argv)
        text = self._fmt(cpu.d[1], cpu.d[2])
        self.out += text
        cpu.d[0] = len(text)

    def _dos_966(self, cpu: Cpu) -> None:                    # ParsePatternNoCase(d1 src, d2 dest, d3 len): 1 a wildcard
        s = self._cbytes(cpu.d[1])
        cpu.load(cpu.d[2], s[:max(cpu.d[3] - 1, 0)] + b"\0")
        cpu.d[0] = 1 if any(c in s for c in b"#?*(|[~%") else 0

    _dos_840 = _dos_966                                      # ParsePattern

    def _dos_972(self, cpu: Cpu) -> None:                    # MatchPatternNoCase(d1 pattern, d2 string)
        from .amigainstall import pattern_regex
        cpu.d[0] = _DOSTRUE if pattern_regex(self._cstr(cpu.d[1])).match(self._cstr(cpu.d[2])) else 0

    _dos_846 = _dos_972                                      # MatchPattern

    # -- utility.library: the arithmetic a C compiler calls on a 68000
    def _utility_138(self, cpu: Cpu) -> None:                # SMult32
        cpu.d[0] = (_sx32(cpu.d[0]) * _sx32(cpu.d[1])) & 0xFFFFFFFF

    _utility_144 = _utility_138                              # UMult32 (the low 32 bits are the same)

    def _utility_150(self, cpu: Cpu) -> None:                # SDivMod32(d0 / d1): d0 quotient, d1 remainder
        a, b = _sx32(cpu.d[0]), _sx32(cpu.d[1])
        if b == 0:
            raise AmigaDosError(f"{self.program} divides by zero")
        q = abs(a) // abs(b) * (1 if (a < 0) == (b < 0) else -1)
        cpu.d[0], cpu.d[1] = q & 0xFFFFFFFF, (a - q * b) & 0xFFFFFFFF

    def _utility_156(self, cpu: Cpu) -> None:                # UDivMod32
        if not cpu.d[1]:
            raise AmigaDosError(f"{self.program} divides by zero")
        cpu.d[0], cpu.d[1] = cpu.d[0] // cpu.d[1], cpu.d[0] % cpu.d[1]

    def _utility_198(self, cpu: Cpu) -> None:                # SMult64: d0:d1 (high, low)
        p = (_sx32(cpu.d[0]) * _sx32(cpu.d[1])) & 0xFFFFFFFFFFFFFFFF
        cpu.d[0], cpu.d[1] = p >> 32, p & 0xFFFFFFFF

    def _utility_204(self, cpu: Cpu) -> None:                # UMult64
        p = cpu.d[0] * cpu.d[1]
        cpu.d[0], cpu.d[1] = p >> 32, p & 0xFFFFFFFF

    def _utility_162(self, cpu: Cpu) -> None:                # Stricmp(a0, a1)
        a, b = self._cstr(cpu.a[0]).casefold(), self._cstr(cpu.a[1]).casefold()
        cpu.d[0] = 0 if a == b else (0xFFFFFFFF if a < b else 1)

    def _utility_168(self, cpu: Cpu) -> None:                # Strnicmp(a0, a1, d0)
        n = cpu.d[0]
        a, b = self._cstr(cpu.a[0])[:n].casefold(), self._cstr(cpu.a[1])[:n].casefold()
        cpu.d[0] = 0 if a == b else (0xFFFFFFFF if a < b else 1)

    def _utility_174(self, cpu: Cpu) -> None:                # ToUpper
        cpu.d[0] = ord(chr(cpu.d[0] & 0xFF).upper()[0]) & 0xFF

    def _utility_180(self, cpu: Cpu) -> None:                # ToLower
        cpu.d[0] = ord(chr(cpu.d[0] & 0xFF).lower()[0]) & 0xFF

    # -- requesters: answered with their first button ("OK", "Continue")
    def _yes(self, cpu: Cpu) -> None:
        cpu.d[0] = 1

    _intuition_348 = _intuition_588 = _reqtools_66 = _yes    # AutoRequest, EasyRequestArgs, rtEZRequestA

    def _intuition_96(self, cpu: Cpu) -> None:               # DisplayBeep
        cpu.d[0] = 0

    def _intuition_204(self, cpu: Cpu) -> None:              # OpenWindow: none here
        cpu.d[0] = 0

    _intuition_606 = _intuition_204                          # OpenWindowTagList

    def _graphics_270(self, cpu: Cpu) -> None:               # WaitTOF
        cpu.d[0] = 0

    # -- running
    def start(self, exe: bytes, args: str) -> int:
        cpu = self.cpu
        try:
            addrs = load_hunks(exe, cpu, _CODE, seglist=True)
        except (ImagerError, struct.error, IndexError) as exc:
            raise AmigaDosError(f"{self.program} is not an Amiga program ({exc})") from None
        if max(addrs) + cpu.r32(max(addrs) - 8) >= _HEAP:
            raise AmigaDosError(f"{self.program} is too big")
        cpu.w32(self.proc + 128, (addrs[0] - 4) >> 2)
        line = (args.strip() + "\n").encode("latin-1", "replace")
        self.argstr = self._put_cstr(line)
        cpu.a[0] = self.argstr
        cpu.d[0] = len(line)
        cpu.m68020 = True
        try:
            cpu.call(addrs[0], sp=_STACK - 16, max_steps=self.max_steps)
            rc = _sx32(cpu.d[0])
        except _Exit as e:
            rc = e.rc
        except CpuError as exc:
            raise AmigaDosError(f"{self.program} crashed here: {exc}") from None
        self.flush()
        return rc


def run(exe: bytes, args: str, fs: Any, cwd: str = "", disks: Optional[list[Any]] = None, name: str = "program",
        shell: Optional[Callable[[str, str], int]] = None, max_steps: int = _MAX_STEPS) -> tuple[int, str]:
    """Run the program ``exe`` (an AmigaDOS executable) with the arguments ``args``, in the drawer ``cwd`` of ``fs`` (the
    installer's volumes); ``disks``: the floppies (trackdisk.device's units); ``shell``: runs a command the program runs
    (``Execute`` / ``System``) - ``shell(line, cwd)``, its return code. Returns ``(return code, what it printed)``."""
    m = _Machine(fs, cwd, disks, name, shell, max_steps)
    rc = m.start(exe, args)
    return rc, m.out.decode("latin-1", "replace")


# --------------------------------------------------------------------------- Patcher (Egon / Harry / Wepl): its modules run here

PCH_FILECOUNT, PCH_ADAPTOR, PCH_DISKNAME, PCH_CHECKDISK, PCH_FILENAME, PCH_SPECIAL, PCH_CHANGES, PCH_PROCESSING = range(0x80004450, 0x80004458)
PCH_NAME, PCH_MINVERSION, PCH_LOADOFFSET, PCH_INIT, PCH_STATE, PCH_STATE2, PCH_APPENDSPACE, PCH_SAVEDATA = range(0x80004458, 0x80004460)
PCH_DATALENGTH, PCH_BOOTBLOCK, PCH_FINISH, PCH_ERRORINPARAMETER, PCH_SAVEPGM = range(0x80004460, 0x80004465)


def crc16(data: bytes) -> int:
    """Patcher's PTB_CRC16: CRC-16 (polynomial $A001, reflected) from 0."""
    c = 0
    for b in data:
        c ^= b
        for _ in range(8):
            c = (c >> 1) ^ 0xA001 if c & 1 else c >> 1
    return c


def run_patcher(module: bytes, fs: Any, disks: list[Any], name: str = "Patcher module", unit: int = 0) -> list[str]:
    """Patcher's run of a module ("-p<Game>.Imager -sDF0:"): its tags read, then the cycles as Patcher makes them - a disk name
    (the volume the file goes to: an Assign), the module's load routine (PCH_SPECIAL: an imager reading the disk with
    trackdisk.device - TD_RAWREAD), processing, the changes table, and the file saved; or a file of the disk loaded, changed and
    saved; or data saved. Cycle k reads disk k (one disk: always it). What the module displays is returned."""
    m = _Machine(fs, "", disks, name)
    cpu = m.cpu
    try:
        addrs = load_hunks(module, cpu, _CODE, seglist=True)
    except (ImagerError, struct.error, IndexError) as exc:
        raise AmigaDosError(f"{name} is not a Patcher module ({exc})") from None
    m.heap = max(m.heap, (max(addrs) + cpu.r32(max(addrs) - 8) + 0x200 + 15) & ~15)
    hp = addrs[0]
    if cpu.r32(hp + 8) != 0x50544348:                          # ("PTCH" after moveq / rts / the tag table's address)
        raise AmigaDosError(f"{name} is not a Patcher module")
    tags: dict[int, int] = {}
    t = cpu.r32(hp + 4)
    while cpu.r32(t):
        tags.setdefault(cpu.r32(t), cpu.r32(t + 4))
        t += 8
    said: list[str] = []
    ptb = m._alloc(0x60)
    space = m._alloc(0xC000)
    io = m._alloc(64)
    cur = {"disk": None}

    def routine(fn: Callable[[Cpu], None]) -> int:
        a = m._alloc(4)
        cpu.hook(a, fn)
        return a

    def opendevice(c: Cpu) -> None:
        if c.d[0] != 0:
            c.d[0] = 0xFFFFFFFF                                # (no destination drive: the files go to the volume named)
            return
        c.w32(io + 20, m.trackdisk)
        c.w32(io + 24, unit)
        c.w32(ptb + 0x28, io)
        c.d[0] = 0

    def zero(c: Cpu) -> None:
        c.d[0] = 0

    def fail(c: Cpu) -> None:
        c.d[0] = 0xFFFFFFFF

    def crc(c: Cpu) -> None:
        c.d[0] = crc16(bytes(c.m[c.a[0] & 0xFFFFFF:(c.a[0] & 0xFFFFFF) + (c.d[0] & 0xFFFFFF)]))

    def display(c: Cpu) -> None:
        said.append(m._cstr(c.a[0]).replace("\n", " ").strip())
        c.d[0] = 0

    cpu.w32(ptb + 0x00, _DOS)
    cpu.w32(ptb + 0x08, m.libs["intuition.library"])
    cpu.w32(ptb + 0x0C, m.libs["graphics.library"])
    cpu.w32(ptb + 0x24, routine(opendevice))
    cpu.w32(ptb + 0x30, space)
    cpu.w32(ptb + 0x34, routine(fail))                         # (format the destination: there is none)
    cpu.w32(ptb + 0x38, routine(zero))                         # (a requester: "Ok")
    cpu.w32(ptb + 0x44, routine(zero))
    cpu.w32(ptb + 0x48, routine(zero))
    cpu.w32(ptb + 0x4C, routine(crc))
    cpu.w32(ptb + 0x50, routine(display))
    real_disk = m._disk
    m._disk = lambda u: cur["disk"] if u == unit and cur["disk"] is not None else real_disk(u)   # type: ignore[method-assign]

    def call(at: int, cycle: int) -> int:
        cpu.d[6] = cycle
        cpu.a[5] = ptb
        try:
            cpu.call(at, sp=_STACK - 16, max_steps=m.max_steps)
        except CpuError as exc:
            raise AmigaDosError(f"{name} crashed here: {exc}") from None
        return _sx32(cpu.d[0])

    def entry(tag: int, k: int) -> int:
        arr = tags.get(tag)
        return cpu.r32(arr + 4 * k) if arr else 0

    def fault(why: str) -> AmigaDosError:
        if tags.get(PCH_ERRORINPARAMETER):
            call(tags[PCH_ERRORINPARAMETER], 0)
        return AmigaDosError(f"{name}: {why}" + (f" ({' / '.join(said[-2:])})" if said else ""))

    if PCH_INIT in tags and call(tags[PCH_INIT], 0):
        raise fault("its initialisation failed")
    n = tags.get(PCH_FILECOUNT, 0)
    for k in range(n):
        if disks:
            cur["disk"] = disks[min(k, len(disks) - 1)]
        vol = m._cstr(entry(PCH_DISKNAME, k)) if entry(PCH_DISKNAME, k) else ""
        fname = m._cstr(entry(PCH_FILENAME, k)) if entry(PCH_FILENAME, k) else ""
        full = f"{vol}:{fname}" if vol else fname
        full_at = m._put_cstr(full.encode("latin-1") + bytes(256))
        cpu.w32(ptb + 0x18, full_at)
        cpu.w32(ptb + 0x14, full_at + len(vol) + 1 if vol else full_at)
        cpu.w32(ptb + 0x20, m._put_cstr(vol.encode("latin-1") + b":"))
        if vol and not m._kind(vol + ":"):
            raise fault(f"the volume {vol}: is not there")
        if entry(PCH_CHECKDISK, k) and call(entry(PCH_CHECKDISK, k), k):
            raise fault(f"cycle {k + 1}: the disk is not the one it wants")
        if entry(PCH_SPECIAL, k):
            size = entry(PCH_DATALENGTH, k)
            if not size:
                raise fault("a load routine without a length")
            buf = m._alloc((size + 7) & ~7)
            cpu.w32(ptb + 0x04, buf)
            cpu.w32(ptb + 0x1C, size)
            if call(entry(PCH_SPECIAL, k), k):
                raise fault(f"cycle {k + 1}: its load routine failed")
        elif entry(PCH_SAVEDATA, k):
            buf, size = entry(PCH_SAVEDATA, k), entry(PCH_DATALENGTH, k)
            cpu.w32(ptb + 0x04, buf)
            cpu.w32(ptb + 0x1C, size)
        elif entry(PCH_BOOTBLOCK, k):
            raise fault("it writes a bootblock to a disk (not done here)")
        else:
            data = m._read(full)
            if data is None:
                raise fault(f"no file {full}")
            off = entry(PCH_LOADOFFSET, k) if PCH_LOADOFFSET in tags else 0
            room = entry(PCH_APPENDSPACE, k) if PCH_APPENDSPACE in tags else 0
            want = entry(PCH_DATALENGTH, k) if PCH_DATALENGTH in tags else 0
            if want and want != len(data):
                raise fault(f"{full} is {len(data)} bytes, not {want}: another version")
            size = off + len(data) + room
            buf = m._alloc((size + 7) & ~7)
            cpu.load(buf + off, data)
            cpu.w32(ptb + 0x04, buf)
            cpu.w32(ptb + 0x1C, size)
        if not entry(PCH_SAVEDATA, k):
            if entry(PCH_PROCESSING, k) and call(entry(PCH_PROCESSING, k), k):
                raise fault(f"cycle {k + 1}: its processing failed")
            if entry(PCH_CHANGES, k):
                _changes(cpu, cpu.r32(ptb + 0x04), entry(PCH_CHANGES, k))
            if entry(PCH_SAVEPGM, k) and call(entry(PCH_SAVEPGM, k), k):
                raise fault(f"cycle {k + 1}: its save routine failed")
        if fname:
            buf, size = cpu.r32(ptb + 0x04), cpu.r32(ptb + 0x1C)
            fs.write(m._cstr(cpu.r32(ptb + 0x18)), bytes(cpu.m[buf:buf + size]))
    if PCH_FINISH in tags:
        call(tags[PCH_FINISH], n)
    m.flush()
    return said + ([m.out.decode("latin-1", "replace").strip()] if m.out.strip() else [])


def _changes(cpu: Cpu, buf: int, table: int) -> None:
    """MAKECOMPTAB's table: longs of an offset (bits 0-20) and a kind (bits 21-23): 0 one byte after it, $800000 a count byte and
    that many bytes (to an even address), $400000 the end."""
    t = table
    while True:
        v = cpu.r32(t)
        t += 4
        kind, off = v & 0xE00000, v & 0x1FFFFF
        if kind == 0x400000:
            return
        if kind == 0:
            cpu.w8(buf + off, cpu.r8(t))
        elif kind == 0x800000:
            n = cpu.r8(t)
            t += 1
            cpu.load(buf + off, bytes(cpu.m[t:t + n]))
            t = (t + n) & ~1
        else:
            raise AmigaDosError("a changes table Patcher does not know")
