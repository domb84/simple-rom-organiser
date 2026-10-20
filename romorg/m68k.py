"""A Motorola 68000 interpreter (stdlib only), enough to run the small Amiga programs WHDLoad installs ship - the RawDIC imagers
(``.islave``) that turn a floppy's tracks into files. Integer instructions of the 68000 and the few 68020 ones such code uses (long
branches, ``EXTB``, 32-bit ``MULx`` / ``DIVx``, scaled indexes); no FPU, no MMU, no exceptions but errors.

    cpu = Cpu(1 << 24)
    cpu.load(0x10000, code)
    cpu.hooks[0x8000] = handler            # a Python routine "called" with JSR: it returns like RTS
    cpu.call(0x10000, sp=0xF00000)         # runs until the routine returns
"""

from __future__ import annotations

from typing import Callable, Optional

__all__ = ["Cpu", "CpuError"]


class CpuError(Exception):
    """An instruction this interpreter does not do, a trap, a runaway program."""


_MASK = {1: 0xFF, 2: 0xFFFF, 4: 0xFFFFFFFF}
_MSB = {1: 0x80, 2: 0x8000, 4: 0x80000000}
_SIZE2 = {0: 1, 1: 2, 2: 4}                      # size field of most instructions
_MOVE_SIZE = {1: 1, 3: 2, 2: 4}                  # size field of MOVE


def _sx8(v: int) -> int:
    v &= 0xFF
    return v - 0x100 if v & 0x80 else v


def _sx16(v: int) -> int:
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


def _sx32(v: int) -> int:
    v &= 0xFFFFFFFF
    return v - 0x100000000 if v & 0x80000000 else v


def _sx(v: int, size: int) -> int:
    return _sx8(v) if size == 1 else _sx16(v) if size == 2 else _sx32(v)


_RETURN = 0xFFFFFE                               # the address a top-level call returns to


class Cpu:
    def __init__(self, memory: int = 1 << 24) -> None:
        self.m = bytearray(memory)
        self.amask = memory - 1
        self.d = [0] * 8
        self.a = [0] * 8
        self.pc = 0
        self.x = self.n = self.z = self.v = self.c = 0
        self.sr_hi = 0x20                        # supervisor, interrupts off: nothing here uses them
        self.hooks: dict[int, Callable[["Cpu"], None]] = {}
        self.m68020 = True                       # the 68020's index forms (scale, full extension words); False: a 68000's
        self.steps = 0
        self._table: list[Optional[Callable[[], None]]] = [None] * 65536

    # ------------------------------------------------------------------ memory
    def load(self, addr: int, data: bytes) -> None:
        self.m[addr:addr + len(data)] = data

    def r8(self, a: int) -> int:
        return self.m[a & self.amask]

    def r16(self, a: int) -> int:
        a &= self.amask
        m = self.m
        return (m[a] << 8) | m[a + 1]

    def r32(self, a: int) -> int:
        a &= self.amask
        return int.from_bytes(self.m[a:a + 4], "big")

    def w8(self, a: int, v: int) -> None:
        self.m[a & self.amask] = v & 0xFF

    def w16(self, a: int, v: int) -> None:
        a &= self.amask
        self.m[a] = (v >> 8) & 0xFF
        self.m[a + 1] = v & 0xFF

    def w32(self, a: int, v: int) -> None:
        a &= self.amask
        self.m[a:a + 4] = (v & 0xFFFFFFFF).to_bytes(4, "big")

    def rd(self, a: int, size: int) -> int:
        return self.m[a & self.amask] if size == 1 else self.r16(a) if size == 2 else self.r32(a)

    def wr(self, a: int, size: int, v: int) -> None:
        if size == 1:
            self.m[a & self.amask] = v & 0xFF
        elif size == 2:
            self.w16(a, v)
        else:
            self.w32(a, v)

    def cstring(self, a: int) -> str:
        a &= self.amask
        end = self.m.index(0, a)
        return self.m[a:end].decode("latin-1")

    # ------------------------------------------------------------------ stack / calls
    def push32(self, v: int) -> None:
        self.a[7] = (self.a[7] - 4) & 0xFFFFFFFF
        self.w32(self.a[7], v)

    def pop32(self) -> int:
        v = self.r32(self.a[7])
        self.a[7] = (self.a[7] + 4) & 0xFFFFFFFF
        return v

    def call(self, addr: int, sp: Optional[int] = None, max_steps: int = 200_000_000) -> None:
        """Run the routine at ``addr`` until it returns (RTS to the top)."""
        if sp is not None:
            self.a[7] = sp
        self.push32(_RETURN)
        self.run(addr, max_steps)

    def run(self, pc: int, max_steps: int = 200_000_000) -> None:
        self.pc = pc
        table, m, amask, decode = self._table, self.m, self.amask, self._decode
        n = 0
        try:
            while True:
                pc = self.pc
                if pc == _RETURN:
                    return
                p = pc & amask
                op = (m[p] << 8) | m[p + 1]
                self.pc = pc + 2
                f = table[op]
                if f is None:
                    f = table[op] = decode(op)
                f()
                n += 1
                if n > max_steps:
                    raise CpuError(f"runs too long (over {max_steps} instructions; at ${pc:06x})")
        finally:
            self.steps += n

    def hook(self, addr: int, fn: Callable[["Cpu"], None]) -> None:
        """A Python routine at ``addr``: code that calls it (JSR / BSR) gets it run, then returns as from an RTS."""
        self.hooks[addr] = fn
        self.w16(addr, 0xA000)                   # (an A-line opcode: the 68000 has none, it marks the hook)

    def fetch16(self) -> int:
        v = self.r16(self.pc)
        self.pc += 2
        return v

    def fetch32(self) -> int:
        v = self.r32(self.pc)
        self.pc += 4
        return v

    # ------------------------------------------------------------------ status register
    @property
    def ccr(self) -> int:
        return (self.x << 4) | (self.n << 3) | (self.z << 2) | (self.v << 1) | self.c

    @ccr.setter
    def ccr(self, v: int) -> None:
        self.x, self.n, self.z, self.v, self.c = (v >> 4) & 1, (v >> 3) & 1, (v >> 2) & 1, (v >> 1) & 1, v & 1

    @property
    def sr(self) -> int:
        return (self.sr_hi << 8) | self.ccr

    @sr.setter
    def sr(self, v: int) -> None:
        self.sr_hi = (v >> 8) & 0xA7
        self.ccr = v & 0xFF

    def cond(self, cc: int) -> bool:
        n, z, v, c = self.n, self.z, self.v, self.c
        if cc == 0: return True
        if cc == 1: return False
        if cc == 2: return not c and not z
        if cc == 3: return bool(c or z)
        if cc == 4: return not c
        if cc == 5: return bool(c)
        if cc == 6: return not z
        if cc == 7: return bool(z)
        if cc == 8: return not v
        if cc == 9: return bool(v)
        if cc == 10: return not n
        if cc == 11: return bool(n)
        if cc == 12: return n == v
        if cc == 13: return n != v
        if cc == 14: return not z and n == v
        return bool(z or n != v)

    def _nz(self, v: int, size: int) -> None:
        self.n = 1 if v & _MSB[size] else 0
        self.z = 1 if (v & _MASK[size]) == 0 else 0

    def _logic(self, v: int, size: int) -> None:
        self._nz(v, size)
        self.v = self.c = 0

    def _add(self, dst: int, src: int, size: int, x: int = 0) -> int:
        m = _MASK[size]
        r = dst + src + x
        res = r & m
        msb = _MSB[size]
        self.c = self.x = 1 if r > m else 0
        self.v = 1 if (~(dst ^ src) & (dst ^ res) & msb) else 0
        self.n = 1 if res & msb else 0
        self.z = 1 if res == 0 else 0
        return res

    def _sub(self, dst: int, src: int, size: int, x: int = 0, setx: bool = True) -> int:
        m = _MASK[size]
        r = dst - src - x
        res = r & m
        msb = _MSB[size]
        c = 1 if r < 0 else 0
        self.c = c
        if setx:
            self.x = c
        self.v = 1 if ((dst ^ src) & (dst ^ res) & msb) else 0
        self.n = 1 if res & msb else 0
        self.z = 1 if res == 0 else 0
        return res

    # ------------------------------------------------------------------ effective addresses
    def _index(self, base: int) -> int:
        ext = self.fetch16()
        r = (ext >> 12) & 7
        x = self.a[r] if ext & 0x8000 else self.d[r]
        x = _sx32(x) if ext & 0x800 else _sx16(x)
        if not self.m68020:                          # (a 68000 ignores bits 8-10)
            return (base + _sx8(ext) + x) & 0xFFFFFFFF
        scale = 1 << ((ext >> 9) & 3)
        if ext & 0x100:                              # 68020 full extension word: base / outer displacements
            bd = 0
            bs = (ext >> 4) & 3
            if ext & 0x80:
                base = 0
            if ext & 0x40:
                x = 0
                scale = 1
            if bs == 2:
                bd = _sx16(self.fetch16())
            elif bs == 3:
                bd = _sx32(self.fetch32())
            iis = ext & 7
            addr = base + bd
            if iis == 0:
                return (addr + x * scale) & 0xFFFFFFFF
            od = 0
            if iis & 3 == 2:
                od = _sx16(self.fetch16())
            elif iis & 3 == 3:
                od = _sx32(self.fetch32())
            if iis & 4:                              # post-indexed
                return (self.r32(addr) + x * scale + od) & 0xFFFFFFFF
            return (self.r32(addr + x * scale) + od) & 0xFFFFFFFF
        return (base + _sx8(ext) + x * scale) & 0xFFFFFFFF

    def ea_addr(self, mode: int, reg: int, size: int) -> int:
        """The address of a memory operand (modes 2-7), consuming its extension words."""
        a = self.a
        if mode == 2:
            return a[reg]
        if mode == 3:
            addr = a[reg]
            a[reg] = (addr + (2 if size == 1 and reg == 7 else size)) & 0xFFFFFFFF
            return addr
        if mode == 4:
            a[reg] = (a[reg] - (2 if size == 1 and reg == 7 else size)) & 0xFFFFFFFF
            return a[reg]
        if mode == 5:
            return (a[reg] + _sx16(self.fetch16())) & 0xFFFFFFFF
        if mode == 6:
            return self._index(a[reg])
        if reg == 0:
            return _sx16(self.fetch16()) & 0xFFFFFFFF
        if reg == 1:
            return self.fetch32()
        if reg == 2:
            base = self.pc
            return (base + _sx16(self.fetch16())) & 0xFFFFFFFF
        if reg == 3:
            return self._index(self.pc)
        raise CpuError(f"bad addressing mode 7/{reg} at ${self.pc:06x}")

    def loc(self, mode: int, reg: int, size: int) -> tuple[int, int]:
        """An operand's place: (0, Dn) / (1, An) / (2, address) / (3, immediate value)."""
        if mode == 0:
            return 0, reg
        if mode == 1:
            return 1, reg
        if mode == 7 and reg == 4:
            if size == 4:
                return 3, self.fetch32()
            v = self.fetch16()
            return 3, (v & 0xFF if size == 1 else v)
        return 2, self.ea_addr(mode, reg, size)

    def get(self, loc: tuple[int, int], size: int) -> int:
        k, v = loc
        if k == 0:
            return self.d[v] & _MASK[size]
        if k == 1:
            return self.a[v] & _MASK[size]
        if k == 2:
            return self.rd(v, size)
        return v & _MASK[size]

    def put(self, loc: tuple[int, int], size: int, val: int) -> None:
        k, v = loc
        if k == 0:
            m = _MASK[size]
            self.d[v] = (self.d[v] & ~m & 0xFFFFFFFF) | (val & m)
        elif k == 1:
            self.a[v] = (_sx16(val) & 0xFFFFFFFF) if size == 2 else val & 0xFFFFFFFF
        elif k == 2:
            self.wr(v, size, val)
        else:
            raise CpuError("write to an immediate")

    def read_ea(self, mode: int, reg: int, size: int) -> int:
        return self.get(self.loc(mode, reg, size), size)

    def src(self, mode: int, reg: int, size: int) -> Callable[[], int]:
        """A source operand's reader, made once when the instruction is first met (the common modes without the general path)."""
        d, a = self.d, self.a
        msk = _MASK[size]
        rd = self.r8 if size == 1 else self.r16 if size == 2 else self.r32
        if mode == 0:
            return lambda: d[reg] & msk
        if mode == 1:
            return lambda: a[reg] & msk
        if mode == 2:
            return lambda: rd(a[reg])
        if mode == 3:
            step = 2 if size == 1 and reg == 7 else size

            def postinc() -> int:
                x = a[reg]
                a[reg] = (x + step) & 0xFFFFFFFF
                return rd(x)
            return postinc
        if mode == 4:
            step = 2 if size == 1 and reg == 7 else size

            def predec() -> int:
                x = a[reg] = (a[reg] - step) & 0xFFFFFFFF
                return rd(x)
            return predec
        if mode == 5:
            fetch = self.fetch16
            return lambda: rd(a[reg] + _sx16(fetch()))
        if mode == 7 and reg == 4:
            if size == 4:
                return self.fetch32
            if size == 2:
                return self.fetch16
            return lambda: self.fetch16() & 0xFF
        return lambda: rd(self.ea_addr(mode, reg, size))

    def dst(self, mode: int, reg: int, size: int) -> Callable[[int], None]:
        """A memory destination's writer (modes 2-7): it works out the address (taking its extension words), then writes."""
        a = self.a
        wr = self.w8 if size == 1 else self.w16 if size == 2 else self.w32
        step = 2 if size == 1 and reg == 7 else size
        if mode == 2:
            return lambda v: wr(a[reg], v)
        if mode == 3:
            def postinc(v: int) -> None:
                x = a[reg]
                a[reg] = (x + step) & 0xFFFFFFFF
                wr(x, v)
            return postinc
        if mode == 4:
            def predec(v: int) -> None:
                x = a[reg] = (a[reg] - step) & 0xFFFFFFFF
                wr(x, v)
            return predec
        return lambda v: wr(self.ea_addr(mode, reg, size), v)

    def cond_fn(self, cc: int) -> Callable[[], bool]:
        s = self
        return (lambda: True, lambda: False, lambda: not s.c and not s.z, lambda: bool(s.c or s.z), lambda: not s.c,
                lambda: bool(s.c), lambda: not s.z, lambda: bool(s.z), lambda: not s.v, lambda: bool(s.v), lambda: not s.n,
                lambda: bool(s.n), lambda: s.n == s.v, lambda: s.n != s.v, lambda: not s.z and s.n == s.v,
                lambda: bool(s.z or s.n != s.v))[cc]

    # ------------------------------------------------------------------ decoding
    def _decode(self, op: int) -> Callable[[], None]:
        line = op >> 12
        f = getattr(self, f"_line{line:X}")(op)
        if f is None:
            def bad() -> None:
                raise CpuError(f"instruction ${op:04x} at ${self.pc - 2:06x} is not one this interpreter does")
            return bad
        return f

    # --- line 0: immediates, bit operations, MOVEP
    def _line0(self, op: int):
        mode, reg = (op >> 3) & 7, op & 7
        if op in (0x003C, 0x023C, 0x0A3C, 0x007C, 0x027C, 0x0A7C):
            kind = (op >> 9) & 7
            to_sr = op & 0x40

            def f() -> None:
                imm = self.fetch16()
                cur = self.sr if to_sr else self.ccr
                v = cur | imm if kind == 0 else cur & imm if kind == 1 else cur ^ imm
                if to_sr:
                    self.sr = v
                else:
                    self.ccr = v & 0x1F
            return f
        if op & 0x100:
            dn = (op >> 9) & 7
            if mode == 1:                                    # MOVEP
                an, opm = reg, (op >> 6) & 7

                def movep() -> None:
                    addr = (self.a[an] + _sx16(self.fetch16())) & 0xFFFFFFFF
                    n = 2 if opm in (4, 6) else 4
                    if opm in (4, 5):
                        v = 0
                        for i in range(n):
                            v = (v << 8) | self.r8(addr + 2 * i)
                        self.put((0, dn), n, v)
                    else:
                        v = self.d[dn]
                        for i in range(n):
                            self.w8(addr + 2 * i, v >> (8 * (n - 1 - i)))
                return movep
            return self._bitop((op >> 6) & 3, mode, reg, lambda: self.d[dn])
        kind = (op >> 9) & 7
        if kind == 4:
            return self._bitop((op >> 6) & 3, mode, reg, lambda: self.fetch16() & 0xFF)
        size = _SIZE2.get((op >> 6) & 3)
        if size is None or kind == 7:
            return None

        def f() -> None:
            imm = self.fetch32() if size == 4 else (self.fetch16() & _MASK[size])
            loc = self.loc(mode, reg, size)
            dst = self.get(loc, size)
            if kind == 0:
                r = dst | imm; self._logic(r, size); self.put(loc, size, r)
            elif kind == 1:
                r = dst & imm; self._logic(r, size); self.put(loc, size, r)
            elif kind == 2:
                self.put(loc, size, self._sub(dst, imm, size))
            elif kind == 3:
                self.put(loc, size, self._add(dst, imm, size))
            elif kind == 5:
                r = dst ^ imm; self._logic(r, size); self.put(loc, size, r)
            else:
                self._sub(dst, imm, size, setx=False)
        return f

    def _bitop(self, kind: int, mode: int, reg: int, bitnum: Callable[[], int]):
        def f() -> None:
            n = bitnum()
            if mode == 0:
                b = n & 31
                v = self.d[reg]
                self.z = 0 if v & (1 << b) else 1
                if kind == 1: self.d[reg] = v ^ (1 << b)
                elif kind == 2: self.d[reg] = v & ~(1 << b) & 0xFFFFFFFF
                elif kind == 3: self.d[reg] = v | (1 << b)
                return
            b = n & 7
            loc = self.loc(mode, reg, 1)
            v = self.get(loc, 1)
            self.z = 0 if v & (1 << b) else 1
            if kind == 1: self.put(loc, 1, v ^ (1 << b))
            elif kind == 2: self.put(loc, 1, v & ~(1 << b))
            elif kind == 3: self.put(loc, 1, v | (1 << b))
        return f

    # --- lines 1-3: MOVE
    def _move(self, op: int):
        size = _MOVE_SIZE[op >> 12]
        dreg, dmode, smode, sreg = (op >> 9) & 7, (op >> 6) & 7, (op >> 3) & 7, op & 7
        read = self.src(smode, sreg, size)
        d, a = self.d, self.a
        msb, m = _MSB[size], _MASK[size]
        keep = ~m & 0xFFFFFFFF
        if dmode == 1:
            if size == 2:
                def movea_w() -> None:
                    v = read()
                    a[dreg] = (v - 0x10000) & 0xFFFFFFFF if v & 0x8000 else v
                return movea_w

            def movea() -> None:
                a[dreg] = read()
            return movea
        if dmode == 0:
            def to_d() -> None:
                v = read()
                d[dreg] = (d[dreg] & keep) | v
                self.n = 1 if v & msb else 0
                self.z = 0 if v else 1
                self.v = self.c = 0
            return to_d
        write = self.dst(dmode, dreg, size)

        def f() -> None:
            v = read()
            write(v)
            self.n = 1 if v & msb else 0
            self.z = 0 if v else 1
            self.v = self.c = 0
        return f

    _line1 = _line2 = _line3 = _move

    # --- line 4: miscellaneous
    def _line4(self, op: int):
        mode, reg = (op >> 3) & 7, op & 7
        if op == 0x4E71:
            return lambda: None
        if op == 0x4E75:
            def rts() -> None:
                self.pc = self.pop32()
            return rts
        if op == 0x4E73:
            def rte() -> None:
                sr = self.r16(self.a[7]); self.a[7] = (self.a[7] + 2) & 0xFFFFFFFF
                self.pc = self.pop32(); self.sr = sr
            return rte
        if op == 0x4E77:
            def rtr() -> None:
                ccr = self.r16(self.a[7]); self.a[7] = (self.a[7] + 2) & 0xFFFFFFFF
                self.ccr = ccr & 0x1F; self.pc = self.pop32()
            return rtr
        if op == 0x4E76:
            def trapv() -> None:
                if self.v:
                    raise CpuError(f"TRAPV at ${self.pc - 2:06x}")
            return trapv
        if op == 0x4E70:
            return lambda: None
        if op == 0x4AFC:
            def illegal() -> None:
                raise CpuError(f"ILLEGAL at ${self.pc - 2:06x}")
            return illegal
        if op & 0xFFF0 == 0x4E40:
            def trap() -> None:
                raise CpuError(f"TRAP #{op & 15} at ${self.pc - 2:06x}")
            return trap
        if op & 0xFFF8 == 0x4E50:
            def link() -> None:
                d = _sx16(self.fetch16())
                self.push32(self.a[reg]); self.a[reg] = self.a[7]
                self.a[7] = (self.a[7] + d) & 0xFFFFFFFF
            return link
        if op & 0xFFF8 == 0x4808:
            def link_l() -> None:
                d = _sx32(self.fetch32())
                self.push32(self.a[reg]); self.a[reg] = self.a[7]
                self.a[7] = (self.a[7] + d) & 0xFFFFFFFF
            return link_l
        if op & 0xFFF8 == 0x4E58:
            def unlk() -> None:
                self.a[7] = self.a[reg]; self.a[reg] = self.pop32()
            return unlk
        if op & 0xFFF0 == 0x4E60:
            return lambda: None                              # MOVE USP: one stack here
        if op & 0xFFC0 == 0x4E80:
            def jsr() -> None:
                t = self.ea_addr(mode, reg, 4)
                self.push32(self.pc); self.pc = t
            return jsr
        if op & 0xFFC0 == 0x4EC0:
            def jmp() -> None:
                self.pc = self.ea_addr(mode, reg, 4)
            return jmp
        if op & 0xF1C0 == 0x41C0 and mode > 1:                # (LEA takes no register: $49C0 with one is EXTB.L)
            an = (op >> 9) & 7

            def lea() -> None:
                self.a[an] = self.ea_addr(mode, reg, 4)
            return lea
        if op & 0xF140 == 0x4100:                            # CHK.W / CHK.L
            dn, size = (op >> 9) & 7, 2 if op & 0x80 else 4

            def chk() -> None:
                bound = _sx(self.read_ea(mode, reg, size), size)
                v = _sx(self.d[dn], size)
                if v < 0 or v > bound:
                    raise CpuError(f"CHK at ${self.pc:06x}")
            return chk
        if op & 0xFFC0 == 0x40C0:
            def from_sr() -> None:
                self.put(self.loc(mode, reg, 2), 2, self.sr)
            return from_sr
        if op & 0xFFC0 == 0x42C0:
            def from_ccr() -> None:
                self.put(self.loc(mode, reg, 2), 2, self.ccr)
            return from_ccr
        if op & 0xFFC0 == 0x44C0:
            def to_ccr() -> None:
                self.ccr = self.read_ea(mode, reg, 2) & 0x1F
            return to_ccr
        if op & 0xFFC0 == 0x46C0:
            def to_sr() -> None:
                self.sr = self.read_ea(mode, reg, 2)
            return to_sr
        if op & 0xFF00 in (0x4000, 0x4200, 0x4400, 0x4600) and (op >> 6) & 3 != 3:
            size = _SIZE2[(op >> 6) & 3]
            kind = (op >> 9) & 3

            def unary() -> None:
                loc = self.loc(mode, reg, size)
                v = self.get(loc, size)
                if kind == 0:                                # NEGX
                    z = self.z
                    r = self._sub(0, v, size, self.x)
                    self.z = z if r == 0 else 0
                elif kind == 1:                              # CLR
                    r = 0; self.n = 0; self.z = 1; self.v = self.c = 0
                elif kind == 2:                              # NEG
                    r = self._sub(0, v, size)
                else:                                        # NOT
                    r = ~v & _MASK[size]; self._logic(r, size)
                self.put(loc, size, r)
            return unary
        if op & 0xFFC0 == 0x4800:                            # NBCD
            def nbcd() -> None:
                loc = self.loc(mode, reg, 1)
                self.put(loc, 1, self._sbcd(0, self.get(loc, 1)))
            return nbcd
        if op & 0xFFF8 == 0x4840:
            def swap() -> None:
                v = self.d[reg]
                r = ((v >> 16) | (v << 16)) & 0xFFFFFFFF
                self.d[reg] = r; self._logic(r, 4)
            return swap
        if op & 0xFFF8 == 0x4848:
            return lambda: None                              # BKPT
        if op & 0xFFC0 == 0x4840:
            def pea() -> None:
                self.push32(self.ea_addr(mode, reg, 4))
            return pea
        if op & 0xFFF8 == 0x4880:
            def ext_w() -> None:
                r = _sx8(self.d[reg]) & 0xFFFF
                self.d[reg] = (self.d[reg] & 0xFFFF0000) | r; self._logic(r, 2)
            return ext_w
        if op & 0xFFF8 == 0x48C0:
            def ext_l() -> None:
                r = _sx16(self.d[reg]) & 0xFFFFFFFF
                self.d[reg] = r; self._logic(r, 4)
            return ext_l
        if op & 0xFFF8 == 0x49C0:
            def extb() -> None:
                r = _sx8(self.d[reg]) & 0xFFFFFFFF
                self.d[reg] = r; self._logic(r, 4)
            return extb
        if op & 0xFB80 == 0x4880:                            # MOVEM
            size = 4 if op & 0x40 else 2
            to_regs = bool(op & 0x400)
            return self._movem(mode, reg, size, to_regs)
        if op & 0xFFC0 == 0x4AC0:
            def tas() -> None:
                loc = self.loc(mode, reg, 1)
                v = self.get(loc, 1); self._logic(v, 1); self.put(loc, 1, v | 0x80)
            return tas
        if op & 0xFF00 == 0x4A00:
            size = _SIZE2.get((op >> 6) & 3)

            read = self.src(mode, reg, size)
            msb = _MSB[size]

            def tst() -> None:
                v = read()
                self.n = 1 if v & msb else 0
                self.z = 0 if v else 1
                self.v = self.c = 0
            return tst
        if op & 0xFFC0 == 0x4C00:
            return self._mul_l(mode, reg)
        if op & 0xFFC0 == 0x4C40:
            return self._div_l(mode, reg)
        return None

    def _movem(self, mode: int, reg: int, size: int, to_regs: bool):
        regs = [(self.d, i) for i in range(8)] + [(self.a, i) for i in range(8)]
        lists: dict[int, list] = {}                          # mask -> the registers in it, in transfer order

        def which(mask: int) -> list:
            got = lists.get(mask)
            if got is None:
                if not to_regs and mode == 4:                # -(An): the mask is reversed, the last register first
                    got = [regs[15 - bit] for bit in range(16) if mask & (1 << bit)]
                else:
                    got = [regs[bit] for bit in range(16) if mask & (1 << bit)]
                lists[mask] = got
            return got

        a = self.a
        if not to_regs and mode == 4:
            wr = self.w16 if size == 2 else self.w32

            def push() -> None:
                addr = a[reg]
                for bank, i in which(self.fetch16()):
                    addr = (addr - size) & 0xFFFFFFFF
                    wr(addr, bank[i])
                a[reg] = addr
            return push
        if to_regs:
            m = self.m
            amask = self.amask

            def load() -> None:
                rl = which(self.fetch16())
                addr = a[reg] if mode == 3 else self.ea_addr(mode, reg, size)
                if size == 4:
                    for bank, i in rl:
                        p = addr & amask
                        bank[i] = int.from_bytes(m[p:p + 4], "big")
                        addr = (addr + 4) & 0xFFFFFFFF
                else:
                    for bank, i in rl:
                        p = addr & amask
                        v = (m[p] << 8) | m[p + 1]
                        bank[i] = (v - 0x10000) & 0xFFFFFFFF if v & 0x8000 else v
                        addr = (addr + 2) & 0xFFFFFFFF
                if mode == 3:
                    a[reg] = addr
            return load
        wr = self.w16 if size == 2 else self.w32

        def store() -> None:
            rl = which(self.fetch16())
            addr = self.ea_addr(mode, reg, size)
            for bank, i in rl:
                wr(addr, bank[i])
                addr = (addr + size) & 0xFFFFFFFF
        return store

    def _mul_l(self, mode: int, reg: int):
        def f() -> None:
            ext = self.fetch16()
            dl, dh = (ext >> 12) & 7, ext & 7
            signed, wide = bool(ext & 0x800), bool(ext & 0x400)
            src = self.read_ea(mode, reg, 4)
            a, b = (_sx32(self.d[dl]), _sx32(src)) if signed else (self.d[dl], src)
            r = a * b
            if wide:
                r &= 0xFFFFFFFFFFFFFFFF
                self.d[dh] = r >> 32
                self.d[dl] = r & 0xFFFFFFFF
                self.n = 1 if r >> 63 else 0
                self.z = 1 if r == 0 else 0
                self.v = 0
            else:
                lo = r & 0xFFFFFFFF
                self.v = 1 if (r != (_sx32(lo) if signed else lo)) else 0
                self.d[dl] = lo
                self.n = 1 if lo & 0x80000000 else 0
                self.z = 1 if lo == 0 else 0
            self.c = 0
        return f

    def _div_l(self, mode: int, reg: int):
        def f() -> None:
            ext = self.fetch16()
            dq, dr = (ext >> 12) & 7, ext & 7
            signed, wide = bool(ext & 0x800), bool(ext & 0x400)
            src = self.read_ea(mode, reg, 4)
            if src == 0:
                raise CpuError(f"division by zero at ${self.pc:06x}")
            if wide:
                dividend = (self.d[dr] << 32) | self.d[dq]
                if signed:
                    dividend = dividend - (1 << 64) if dividend >> 63 else dividend
            else:
                dividend = _sx32(self.d[dq]) if signed else self.d[dq]
            divisor = _sx32(src) if signed else src
            q = abs(dividend) // abs(divisor)
            if (dividend < 0) != (divisor < 0):
                q = -q
            rem = dividend - q * divisor
            if (signed and not -0x80000000 <= q <= 0x7FFFFFFF) or (not signed and not 0 <= q <= 0xFFFFFFFF):
                self.v = 1; self.c = 0
                return
            self.d[dq] = q & 0xFFFFFFFF
            if dr != dq:
                self.d[dr] = rem & 0xFFFFFFFF
            self._logic(q & 0xFFFFFFFF, 4)
        return f

    # --- line 5: ADDQ / SUBQ / Scc / DBcc
    def _line5(self, op: int):
        mode, reg = (op >> 3) & 7, op & 7
        if (op >> 6) & 3 == 3:
            cc = (op >> 8) & 15
            if mode == 1:
                test = self.cond_fn(cc)
                d, r16 = self.d, self.r16

                def dbcc() -> None:
                    base = self.pc
                    self.pc = base + 2
                    if cc != 1 and test():
                        return
                    v = (d[reg] - 1) & 0xFFFF
                    d[reg] = (d[reg] & 0xFFFF0000) | v
                    if v != 0xFFFF:
                        self.pc = (base + _sx16(r16(base))) & 0xFFFFFFFF
                return dbcc
            if mode == 7 and reg in (2, 3, 4):              # TRAPcc
                def trapcc() -> None:
                    if reg == 2: self.fetch16()
                    elif reg == 3: self.fetch32()
                    if self.cond(cc):
                        raise CpuError(f"TRAPcc at ${self.pc:06x}")
                return trapcc

            def scc() -> None:
                self.put(self.loc(mode, reg, 1), 1, 0xFF if self.cond(cc) else 0)
            return scc
        size = _SIZE2[(op >> 6) & 3]
        data = (op >> 9) & 7 or 8
        sub = bool(op & 0x100)
        if mode == 1:
            def addq_a() -> None:
                self.a[reg] = (self.a[reg] + (-data if sub else data)) & 0xFFFFFFFF
            return addq_a

        op_ = self._sub if sub else self._add
        if mode == 0:
            d, m = self.d, _MASK[size]
            keep = ~m & 0xFFFFFFFF

            def addq_d() -> None:
                d[reg] = (d[reg] & keep) | op_(d[reg] & m, data, size)
            return addq_d

        def f() -> None:
            loc = self.loc(mode, reg, size)
            v = self.get(loc, size)
            self.put(loc, size, op_(v, data, size))
        return f

    # --- line 6: branches
    def _line6(self, op: int):
        cc = (op >> 8) & 15
        d8 = op & 0xFF
        test = self.cond_fn(cc)
        if 0 < d8 < 0xFF and cc in (4, 5, 6, 7, 10, 11):     # (BCC BCS BNE BEQ BPL BMI, short: the most frequent of all)
            disp8 = _sx8(d8)
            flag, want = {4: ("c", 0), 5: ("c", 1), 6: ("z", 0), 7: ("z", 1), 10: ("n", 0), 11: ("n", 1)}[cc]
            if flag == "z":
                if want:
                    def beq() -> None:
                        if self.z:
                            self.pc += disp8
                    return beq
                def bne() -> None:
                    if not self.z:
                        self.pc += disp8
                return bne
            if flag == "c":
                if want:
                    def bcs() -> None:
                        if self.c:
                            self.pc += disp8
                    return bcs
                def bcc_() -> None:
                    if not self.c:
                        self.pc += disp8
                return bcc_
            if want:
                def bmi() -> None:
                    if self.n:
                        self.pc += disp8
                return bmi
            def bpl() -> None:
                if not self.n:
                    self.pc += disp8
            return bpl
        if 0 < d8 < 0xFF and cc >= 2:                        # (the other short branches)
            disp8 = _sx8(d8)

            def bcc_s() -> None:
                if test():
                    self.pc = (self.pc + disp8) & 0xFFFFFFFF
            return bcc_s

        def f() -> None:
            base = self.pc
            if d8 == 0:
                disp = _sx16(self.fetch16())
            elif d8 == 0xFF and self.m68020:
                disp = _sx32(self.fetch32())
            else:
                disp = _sx8(d8)
            if cc == 1:                                      # BSR
                self.push32(self.pc)
                self.pc = (base + disp) & 0xFFFFFFFF
            elif cc == 0 or test():
                self.pc = (base + disp) & 0xFFFFFFFF
        return f

    # --- line 7: MOVEQ
    def _line7(self, op: int):
        if op & 0x100:
            return None
        dn, v = (op >> 9) & 7, _sx8(op) & 0xFFFFFFFF

        def f() -> None:
            self.d[dn] = v
            self.n = 1 if v & 0x80000000 else 0
            self.z = 1 if v == 0 else 0
            self.v = self.c = 0
        return f

    # --- OR / AND (lines 8, C), SUB / ADD (9, D), CMP / EOR (B)
    def _logic_op(self, op: int, fn: Callable[[int, int], int]):
        dn, opm, mode, reg = (op >> 9) & 7, (op >> 6) & 7, (op >> 3) & 7, op & 7
        size = _SIZE2.get(opm & 3)
        if opm < 3:
            read = self.src(mode, reg, size)
            d, m, msb = self.d, _MASK[size], _MSB[size]
            keep = ~m & 0xFFFFFFFF

            def to_d() -> None:
                v = fn(d[dn] & m, read())
                d[dn] = (d[dn] & keep) | v
                self.n = 1 if v & msb else 0
                self.z = 0 if v else 1
                self.v = self.c = 0
            return to_d

        def to_ea() -> None:
            loc = self.loc(mode, reg, size)
            v = fn(self.get(loc, size), self.d[dn] & _MASK[size])
            self.put(loc, size, v); self._logic(v, size)
        return to_ea

    def _line8(self, op: int):
        opm, mode = (op >> 6) & 7, (op >> 3) & 7
        dn, reg = (op >> 9) & 7, op & 7
        if opm in (3, 7):
            return self._div_w(op, opm == 7)
        if opm == 4 and mode in (0, 1):
            return self._bcd(op, self._sbcd)
        if opm in (5, 6) and mode in (0, 1):
            return None                                      # PACK / UNPK
        return self._logic_op(op, lambda a, b: a | b)

    def _lineC(self, op: int):
        opm, mode = (op >> 6) & 7, (op >> 3) & 7
        dn, reg = (op >> 9) & 7, op & 7
        if opm in (3, 7):
            signed = opm == 7

            def mul() -> None:
                src = self.read_ea(mode, reg, 2)
                a = _sx16(self.d[dn]) if signed else self.d[dn] & 0xFFFF
                b = _sx16(src) if signed else src
                r = (a * b) & 0xFFFFFFFF
                self.d[dn] = r; self._logic(r, 4)
            return mul
        if opm == 4 and mode in (0, 1):
            return self._bcd(op, self._abcd)
        if opm == 5 and mode in (0, 1) or opm == 6 and mode == 1:
            rx, ry = dn, reg

            def exg() -> None:
                if opm == 5 and mode == 0:
                    self.d[rx], self.d[ry] = self.d[ry], self.d[rx]
                elif opm == 5:
                    self.a[rx], self.a[ry] = self.a[ry], self.a[rx]
                else:
                    self.d[rx], self.a[ry] = self.a[ry], self.d[rx]
            return exg
        return self._logic_op(op, lambda a, b: a & b)

    def _div_w(self, op: int, signed: bool):
        dn, mode, reg = (op >> 9) & 7, (op >> 3) & 7, op & 7

        def f() -> None:
            src = self.read_ea(mode, reg, 2)
            if src == 0:
                raise CpuError(f"division by zero at ${self.pc:06x}")
            if signed:
                a, b = _sx32(self.d[dn]), _sx16(src)
                q = abs(a) // abs(b)
                if (a < 0) != (b < 0):
                    q = -q
                r = a - q * b
                if not -0x8000 <= q <= 0x7FFF:
                    self.v = 1; self.c = 0; self.n = 1; self.z = 0
                    return
            else:
                a, b = self.d[dn], src
                q, r = divmod(a, b)
                if q > 0xFFFF:
                    self.v = 1; self.c = 0; self.n = 1; self.z = 0
                    return
            self.d[dn] = ((r & 0xFFFF) << 16) | (q & 0xFFFF)
            self.n = 1 if q & 0x8000 else 0
            self.z = 1 if q & 0xFFFF == 0 else 0
            self.v = self.c = 0
        return f

    # BCD as the chip does it, invalid digits and the undefined N / V too: the binary sum or difference, then a correction
    # from the half carry and the carry (the model the SingleStepTests 68000 vectors agree with in every case)
    def _abcd(self, dst: int, src: int) -> int:
        res = dst + src + self.x
        corf = 6 if (dst ^ src ^ res) & 0x10 or (res & 15) > 9 else 0
        carry = res > 0x99
        if carry:
            corf |= 0x60
        r2 = res + corf
        return self._bcd_flags(res, r2, carry, (~res & r2) & 0x80)

    def _sbcd(self, dst: int, src: int) -> int:
        res = (dst - src - self.x) & 0x1FF
        corf = 6 if (dst ^ src ^ res) & 0x10 else 0
        carry = bool(res & 0x100)
        if carry:
            corf |= 0x60
        r2 = (res - corf) & 0x1FF
        return self._bcd_flags(res, r2, carry or bool(r2 & 0x100), res & ~r2 & 0x80)

    def _bcd_flags(self, res: int, r2: int, carry: bool, v: int) -> int:
        self.c = self.x = 1 if carry else 0
        self.v = 1 if v else 0
        r = r2 & 0xFF
        self.n = 1 if r & 0x80 else 0
        if r:
            self.z = 0
        return r

    def _bcd(self, op: int, fn: Callable[[int, int], int]):
        rx, ry, mem = (op >> 9) & 7, op & 7, bool(op & 8)

        def f() -> None:
            if mem:
                self.a[ry] = (self.a[ry] - (2 if ry == 7 else 1)) & 0xFFFFFFFF
                s = self.r8(self.a[ry])
                self.a[rx] = (self.a[rx] - (2 if rx == 7 else 1)) & 0xFFFFFFFF
                d = self.r8(self.a[rx])
                self.w8(self.a[rx], fn(d, s))
            else:
                r = fn(self.d[rx] & 0xFF, self.d[ry] & 0xFF)
                self.d[rx] = (self.d[rx] & 0xFFFFFF00) | r
        return f

    def _addsub(self, op: int, sub: bool):
        dn, opm, mode, reg = (op >> 9) & 7, (op >> 6) & 7, (op >> 3) & 7, op & 7
        if opm in (3, 7):                                    # ADDA / SUBA
            size = 2 if opm == 3 else 4

            read = self.src(mode, reg, size)
            a = self.a
            if size == 2:
                def adda_w() -> None:
                    v = _sx16(read())
                    a[dn] = (a[dn] - v if sub else a[dn] + v) & 0xFFFFFFFF
                return adda_w

            def adda() -> None:
                v = read()                                   # (first: -(An),An must see the decremented An)
                a[dn] = (a[dn] - v if sub else a[dn] + v) & 0xFFFFFFFF
            return adda
        size = _SIZE2[opm & 3]
        if opm >= 4 and mode in (0, 1):                      # ADDX / SUBX
            rx, ry, mem = dn, reg, mode == 1

            def addx() -> None:
                z = self.z
                if mem:
                    step = 2 if size == 1 and ry == 7 else size
                    self.a[ry] = (self.a[ry] - step) & 0xFFFFFFFF
                    s = self.rd(self.a[ry], size)
                    step = 2 if size == 1 and rx == 7 else size
                    self.a[rx] = (self.a[rx] - step) & 0xFFFFFFFF
                    d = self.rd(self.a[rx], size)
                    r = self._sub(d, s, size, self.x) if sub else self._add(d, s, size, self.x)
                    self.wr(self.a[rx], size, r)
                else:
                    d, s = self.d[rx] & _MASK[size], self.d[ry] & _MASK[size]
                    r = self._sub(d, s, size, self.x) if sub else self._add(d, s, size, self.x)
                    self.put((0, rx), size, r)
                self.z = z if r == 0 else 0
            return addx
        if opm < 3:
            read = self.src(mode, reg, size)
            dd, m = self.d, _MASK[size]
            keep = ~m & 0xFFFFFFFF
            op_ = self._sub if sub else self._add

            def to_d() -> None:
                s = read()
                dd[dn] = (dd[dn] & keep) | op_(dd[dn] & m, s, size)
            return to_d

        def to_ea() -> None:
            loc = self.loc(mode, reg, size)
            d = self.get(loc, size)
            s = self.d[dn] & _MASK[size]
            self.put(loc, size, self._sub(d, s, size) if sub else self._add(d, s, size))
        return to_ea

    def _line9(self, op: int):
        return self._addsub(op, True)

    def _lineD(self, op: int):
        return self._addsub(op, False)

    def _lineB(self, op: int):
        dn, opm, mode, reg = (op >> 9) & 7, (op >> 6) & 7, (op >> 3) & 7, op & 7
        if opm in (3, 7):
            size = 2 if opm == 3 else 4

            read = self.src(mode, reg, size)

            def cmpa() -> None:
                v = read()
                v = _sx16(v) & 0xFFFFFFFF if size == 2 else v
                self._sub(self.a[dn], v, 4, setx=False)
            return cmpa
        size = _SIZE2[opm & 3]
        if opm < 3:
            read = self.src(mode, reg, size)
            m = _MASK[size]

            def cmp() -> None:
                self._sub(self.d[dn] & m, read(), size, setx=False)
            return cmp
        if mode == 1:
            ax, ay = dn, reg

            def cmpm() -> None:
                s = self.rd(self.ea_addr(3, ay, size), size)
                d = self.rd(self.ea_addr(3, ax, size), size)
                self._sub(d, s, size, setx=False)
            return cmpm

        def eor() -> None:
            loc = self.loc(mode, reg, size)
            v = self.get(loc, size) ^ (self.d[dn] & _MASK[size])
            self.put(loc, size, v); self._logic(v, size)
        return eor

    # --- line E: shifts and rotates
    def _lineE(self, op: int):
        if (op >> 6) & 3 == 3:
            if op & 0x800:
                return self._bitfield(op)
            kind, left = (op >> 9) & 3, bool(op & 0x100)
            mode, reg = (op >> 3) & 7, op & 7

            def mem_shift() -> None:
                loc = self.loc(mode, reg, 2)
                self.put(loc, 2, self._shift(kind, left, self.get(loc, 2), 1, 2))
            return mem_shift
        size = _SIZE2[(op >> 6) & 3]
        kind, left = (op >> 3) & 3, bool(op & 0x100)
        cnt_reg, ir, dreg = (op >> 9) & 7, bool(op & 0x20), op & 7

        d, m, msb, bits = self.d, _MASK[size], _MSB[size], 8 * size
        keep = ~m & 0xFFFFFFFF
        if not ir:                                           # (an immediate count: the common case, made without the loop)
            n = cnt_reg or 8
            if kind == 1 and left:                           # LSL
                def lsl() -> None:
                    v = d[dreg] & m
                    r = (v << n) & m
                    d[dreg] = (d[dreg] & keep) | r
                    self.c = self.x = (v >> (bits - n)) & 1
                    self.n = 1 if r & msb else 0
                    self.z = 0 if r else 1
                    self.v = 0
                return lsl
            if kind == 1:                                    # LSR
                def lsr() -> None:
                    v = d[dreg] & m
                    r = v >> n
                    d[dreg] = (d[dreg] & keep) | r
                    self.c = self.x = (v >> (n - 1)) & 1
                    self.n = 1 if r & msb else 0
                    self.z = 0 if r else 1
                    self.v = 0
                return lsr
            if kind == 3:                                    # ROL / ROR
                k = n % bits
                def rot() -> None:
                    v = d[dreg] & m
                    if left:
                        r = ((v << k) | (v >> (bits - k))) & m if k else v
                        self.c = r & 1
                    else:
                        r = ((v >> k) | (v << (bits - k))) & m if k else v
                        self.c = 1 if r & msb else 0
                    d[dreg] = (d[dreg] & keep) | r
                    self.n = 1 if r & msb else 0
                    self.z = 0 if r else 1
                    self.v = 0
                return rot
            if kind == 2:                                    # ROXL / ROXR: a rotation of X and the operand, bits + 1 wide
                w, wm = bits + 1, (1 << (bits + 1)) - 1
                k = n % w
                def rox() -> None:
                    val = (self.x << bits) | (d[dreg] & m)
                    if k:
                        val = ((val << k) | (val >> (w - k))) & wm if left else ((val >> k) | (val << (w - k))) & wm
                    r = val & m
                    d[dreg] = (d[dreg] & keep) | r
                    self.c = self.x = val >> bits
                    self.n = 1 if r & msb else 0
                    self.z = 0 if r else 1
                    self.v = 0
                return rox

        def f() -> None:
            count = (d[cnt_reg] & 63) if ir else (cnt_reg or 8)
            d[dreg] = (d[dreg] & keep) | self._shift(kind, left, d[dreg] & m, count, size)
        return f

    def _shift(self, kind: int, left: bool, v: int, count: int, size: int) -> int:
        bits = size * 8
        m = _MASK[size]
        msb = _MSB[size]
        self.v = 0
        if count == 0:
            self.c = self.x if kind == 2 else 0
            self._nz(v, size)
            return v
        if kind == 0:                                        # ASL / ASR
            if left:
                over = 0
                for _ in range(count):
                    c = 1 if v & msb else 0
                    nv = (v << 1) & m
                    if (nv ^ v) & msb:
                        over = 1
                    v = nv
                self.c = self.x = c
                self.v = over
            else:
                sv = _sx(v, size)
                c = (sv >> (count - 1)) & 1 if count <= bits else (1 if sv < 0 else 0)
                v = (sv >> min(count, bits)) & m
                self.c = self.x = c
        elif kind == 1:                                      # LSL / LSR
            if left:
                c = (v >> (bits - count)) & 1 if count <= bits else 0
                v = (v << count) & m if count < bits else 0
            else:
                c = (v >> (count - 1)) & 1 if count <= bits else 0
                v = v >> count if count < bits else 0
            self.c = self.x = c
        elif kind == 3:                                      # ROL / ROR
            n = count % bits
            if left:
                v = ((v << n) | (v >> (bits - n))) & m if n else v
                self.c = v & 1
            else:
                v = ((v >> n) | (v << (bits - n))) & m if n else v
                self.c = 1 if v & msb else 0
        else:                                                # ROXL / ROXR: a rotation of X and the operand, bits + 1 wide
            w = bits + 1
            k = count % w
            val = (self.x << bits) | v
            if k:
                val = (((val << k) | (val >> (w - k))) if left else ((val >> k) | (val << (w - k)))) & ((1 << w) - 1)
            v = val & m
            self.c = self.x = val >> bits
        self._nz(v, size)
        return v

    def _bitfield(self, op: int):
        kind = (op >> 8) & 7                                 # 0 BFTST 1 BFEXTU 2 BFCHG 3 BFEXTS 4 BFCLR 5 BFFFO 6 BFSET 7 BFINS
        mode, reg = (op >> 3) & 7, op & 7

        def f() -> None:
            ext = self.fetch16()
            off = self.d[(ext >> 6) & 7] if ext & 0x800 else (ext >> 6) & 31
            width = self.d[ext & 7] if ext & 0x20 else ext & 31
            width = (width - 1) % 32 + 1
            dn = (ext >> 12) & 7
            if mode == 0:
                off &= 31
                v = self.d[reg]
                rot = ((v << off) | (v >> (32 - off))) & 0xFFFFFFFF if off else v
                field = rot >> (32 - width)
            else:
                addr = self.ea_addr(mode, reg, 1)
                off = _sx32(off)
                addr = (addr + (off >> 3)) & 0xFFFFFFFF
                boff = off & 7
                big = int.from_bytes(bytes(self.r8(addr + i) for i in range(5)), "big")
                field = (big >> (40 - boff - width)) & ((1 << width) - 1)
            self.n = (field >> (width - 1)) & 1
            self.z = 1 if field == 0 else 0
            self.v = self.c = 0
            if kind == 1:
                self.d[dn] = field
            elif kind == 3:
                self.d[dn] = (field - (1 << width) if field >> (width - 1) else field) & 0xFFFFFFFF
            elif kind == 5:
                pos = 0
                while pos < width and not (field >> (width - 1 - pos)) & 1:
                    pos += 1
                self.d[dn] = (off + pos) & 0xFFFFFFFF
            elif kind in (2, 4, 6, 7):
                fm = (1 << width) - 1
                new = (field ^ fm if kind == 2 else 0 if kind == 4 else fm if kind == 6 else self.d[dn] & fm)
                if kind == 7:
                    self.n = (new >> (width - 1)) & 1
                    self.z = 1 if new == 0 else 0
                if mode == 0:
                    shift = 32 - off - width
                    mask32 = 0xFFFFFFFF
                    fieldmask = ((fm << shift) if shift >= 0 else (fm >> -shift)) & mask32
                    if off + width > 32:                    # wraps around the register
                        lo = (off + width) - 32
                        fieldmask = ((fm >> lo) | (fm << (32 - lo))) & mask32
                        val = ((new >> lo) | (new << (32 - lo))) & mask32
                    else:
                        val = (new << shift) & mask32
                    self.d[reg] = (self.d[reg] & ~fieldmask & mask32) | (val & fieldmask)
                else:
                    sh = 40 - boff - width
                    big = (big & ~(fm << sh)) | (new << sh)
                    for i, b in enumerate(big.to_bytes(5, "big")):
                        self.w8(addr + i, b)
        return f

    def _lineA(self, op: int):
        def hook() -> None:
            h = self.hooks.get(self.pc - 2)
            if h is None:
                raise CpuError(f"instruction ${op:04x} at ${self.pc - 2:06x} (line A)")
            ret = self.pop32()
            h(self)
            self.pc = ret
        return hook

    def _lineF(self, op: int):
        return None
