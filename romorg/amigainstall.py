"""Install a WHDLoad game from its ADF disks, without an Amiga: run the install's own script, then pack the result as an ``.lha``.

A WHDLoad install (``romorg.whdinstalls``) carries an ``Install`` script in the Amiga Installer language (a small Lisp). This module
runs that script here: the floppies it asks for (``askdisk``, ``"HL1:"``) are the ADF images whose disk label is that name
(``romorg.adf``), the drawer it installs to is a folder in memory, and the questions it asks get their default answers (the version
question: the one given). What a script copies off AmigaDOS disks (``copyfiles``, ``rename``, ``delete`` ...) works; what needs an
Amiga program does not and stops the install with the reason - ``RawDIC`` / ``Patcher`` (the disks' own format, which an ADF of a
crack does not have), ``xfdDecrunch``. ``DIC`` (a whole-disk image) is done here: the image is the ADF itself.

    result = install(package_dir, disks)        # package_dir: the unpacked install, disks: [path of each ADF]
    result.files    {"MonkeyIsland/MonkeyIsland.slave": b"...", ...}
    write_lha(path, result.files)               # the .lha PUAE (RetroArch) launches with its WHDLoad support

Whether the game then runs is up to its slave: many accept only the original release (a crack may be refused with "unsupported
version" when it starts).
"""

from __future__ import annotations

import os
import re
import struct
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from . import ipf, rawdic
from .adf import AdfDisk, AdfError
from .lha import crc16

__all__ = ["install", "install_archive", "Package", "version_choices", "InstallResult", "InstallError", "MemDisk", "write_lha", "pattern_regex"]


class InstallError(Exception):
    """The install cannot be done here (a step that needs an Amiga program, a disk that is missing, the script aborted)."""


class _Exit(Exception):
    pass


# --------------------------------------------------------------------------- the Installer language: reading

@dataclass
class _Sym:
    name: str

    def __repr__(self) -> str:
        return self.name


def _parse(text: str) -> list:
    """The forms of a script: lists, strings, numbers, symbols."""
    pos, n = 0, len(text)
    out: list = []
    stack: list[list] = [out]
    while pos < n:
        c = text[pos]
        if c in " \t\r\n\f":
            pos += 1
        elif c == ";":
            while pos < n and text[pos] != "\n":
                pos += 1
        elif c == "(":
            new: list = []
            stack[-1].append(new)
            stack.append(new)
            pos += 1
        elif c == ")":
            if len(stack) > 1:
                stack.pop()
            pos += 1
        elif c in "\"'":
            q = c
            pos += 1
            buf = []
            while pos < n and text[pos] != q:
                ch = text[pos]
                if ch == "\\" and pos + 1 < n:
                    pos += 1
                    e = text[pos]
                    buf.append({"n": "\n", "t": "\t", '"': '"', "'": "'", "\\": "\\", "r": "\r"}.get(e, e))
                else:
                    buf.append(ch)
                pos += 1
            pos += 1
            stack[-1].append("".join(buf))
        else:
            start = pos
            while pos < n and text[pos] not in " \t\r\n\f();\"":
                pos += 1
            tok = text[start:pos]
            if re.fullmatch(r"-?\d+", tok):
                stack[-1].append(int(tok))
            elif re.fullmatch(r"\$[0-9a-fA-F]+", tok):
                stack[-1].append(int(tok[1:], 16))
            elif re.fullmatch(r"%[01]+", tok):
                stack[-1].append(int(tok[1:], 2))
            else:
                stack[-1].append(_Sym(tok.lower()))
    return out


# --------------------------------------------------------------------------- AmigaDOS patterns

def pattern_regex(pat: str) -> "re.Pattern[str]":
    """An AmigaDOS pattern (``#?``, ``?``, ``#x``, ``(a|b)``, ``~(x)``, ``[a-z]``, ``%``) as a regex (case-insensitive, whole name)."""
    def conv(p: str, i: int, stop: str = "") -> tuple[str, int]:
        out = ""
        while i < len(p):
            c = p[i]
            if stop and c in stop:
                return out, i
            if c == "#":
                i += 1
                if i < len(p) and p[i] == "(":
                    inner, i = conv(p, i + 1, ")")
                    out += f"(?:{inner})*"
                    i += 1
                elif i < len(p) and p[i] == "[":
                    j = p.index("]", i)
                    out += p[i:j + 1] + "*"
                    i = j + 1
                elif i < len(p) and p[i] == "?":
                    out += ".*"
                    i += 1
                elif i < len(p):
                    out += re.escape(p[i]) + "*"
                    i += 1
            elif c == "?":
                out += "."
                i += 1
            elif c == "*":
                out += ".*"
                i += 1
            elif c == "~" and i + 1 < len(p) and p[i + 1] == "(":
                inner, i = conv(p, i + 2, ")")
                out += f"(?!(?:{inner})$).*"
                i += 1
            elif c == "(":
                inner, i = conv(p, i + 1, ")")
                out += f"(?:{inner})"
                i += 1
            elif c == "|":
                out += "|"
                i += 1
            elif c == "[":
                j = p.find("]", i)
                if j < 0:
                    out += re.escape(c)
                    i += 1
                else:
                    out += p[i:j + 1].replace("~", "^", 1) if p[i + 1:i + 2] == "~" else p[i:j + 1]
                    i = j + 1
            elif c == "%":
                i += 1
            elif c == "'" and i + 1 < len(p):
                out += re.escape(p[i + 1])
                i += 2
            else:
                out += re.escape(c)
                i += 1
        return out, i
    rx, _ = conv(pat, 0)
    while ".*.*" in rx:
        rx = rx.replace(".*.*", ".*")              # ("#?#?": one wildcard - two in a row backtrack without end on a long name)
    return re.compile(rf"^(?:{rx})$", re.IGNORECASE)


# --------------------------------------------------------------------------- the files the script sees

class MemDisk:
    """A volume made of files in memory (a CD's files, unpacked from an archive): what :class:`romorg.adf.AdfDisk` offers a script."""

    def __init__(self, label: str, files: dict[str, bytes]) -> None:
        self.label = label
        self._files = {k.strip("/"): v for k, v in files.items()}
        self.data = b""

    def files(self) -> list[tuple[str, int]]:
        return sorted((k, len(v)) for k, v in self._files.items())

    def read(self, path: str) -> bytes:
        low = path.strip("/").casefold()
        for k, v in self._files.items():
            if k.casefold() == low:
                return v
        raise AdfError(f"no such file: {path}")


class RawDisk:
    """A floppy image that is no AmigaDOS disk (a game's own format, a cracker's track loader): DIC can image it whole, a script that
    reads files from it is told why it cannot."""

    def __init__(self, data: bytes, reason: str) -> None:
        self.label = ""
        self.data = data
        self.reason = reason

    def files(self) -> list[tuple[str, int]]:
        return []

    def read(self, path: str) -> bytes:
        raise AdfError(f"{self.reason} (the disk is not an AmigaDOS disk)")


def floppy(d: Any, n: int = 0) -> Any:
    """A floppy for an install: an ADF or an IPF (path or bytes). Its files when it is an AmigaDOS disk (an IPF's standard tracks
    made into an ADF), always its tracks (``.tracks``: what RawDIC reads - an IPF's as they are, an ADF's MFM-encoded)."""
    name = Path(d).name if isinstance(d, (str, os.PathLike)) else f"disk {n + 1}"
    data = Path(d).read_bytes() if isinstance(d, (str, os.PathLike)) else bytes(d)
    tracks: Any
    if ipf.is_ipf(data):
        try:
            tracks = ipf.IpfDisk(data)
        except ipf.IpfError as exc:
            raise InstallError(f"{name}: {exc}") from None
        adf, bad = rawdic.standard_image(tracks)
        if bad:                                         # (a disk in the game's own format: only RawDIC reads it)
            disk: Any = RawDisk(b"", f"{name}: {len(bad)} of its tracks are in the game's own format")
            disk.tracks = tracks
            return disk
        data = adf
    else:
        tracks = None
    try:
        disk = AdfDisk(data)
    except AdfError as exc:
        if len(data) not in (901120, 1802240):
            raise InstallError(f"{name}: {exc} (the disk is not an AmigaDOS disk)") from None
        disk = RawDisk(data, f"{name}: {exc}")             # (whole-disk installs - DIC - image it all the same)
    disk.tracks = tracks or rawdic.AdfDisk(data)
    return disk


_CD_RE = re.compile(r"\bCD(?:TV|32)?(?![a-z])", re.IGNORECASE)    # ("CD version", "CD32", "CD³²", "CDTV")
_HD = "RomorgHD:"                                      # (the hard disk the game is installed to: no floppy has that name)
_HD_NAMES = {"romorghd", "work", "sys", "ram", "t", "env", "envarc", "dh0", "dh1", "hd0", "hd1"}


class _Fs:
    """Volumes: the ADFs by label (read-only), the install package (the current drawer) and ``WORK:`` (what gets installed)."""

    def __init__(self, package: Path, disks: list[AdfDisk]) -> None:
        self.package = package
        self.order = list(disks)                      # (in their order: "Disk 1 of 3" ... - DIC's FD=, a disk named by its number)
        self.disks: dict[str, AdfDisk] = {}
        for n, d in enumerate(disks):                 # (by label; disks without one, or with the same one, by their order too)
            key = d.label.casefold() if d.label.strip() and d.label.casefold() not in self.disks else f"\0disk{n + 1}"
            self.disks[key] = d
        self.current: Optional[str] = None
        self.asked: list[Any] = []                    # the disks the script has asked for, in that order ("the k-th disk asked is disk k")
        self.guessed: set[str] = set()                # volumes matched only by that order
        self.work: dict[str, bytes] = {}                 # "Game/data/file" -> bytes (no volume, "/" separated)
        self.work_dirs: set[str] = set()

    def split(self, path: str) -> tuple[str, str]:
        """``("vol", "rest")``; vol "" = the current drawer (the package). ``":file"`` (no volume name) is the disk in the drive:
        the ADF that has that file (the one found last when it has not)."""
        if ":" in path:
            v, rest = path.split(":", 1)
            v, rest = v.strip().casefold(), rest.strip("/").lstrip(":")
            if v == "" or v in _DRIVES:                 # (":file", "DF0:file": the disk in the drive)
                hit = next((lab for lab, d in self.disks.items() if rest and _has(d, rest)), None)
                if hit is not None:
                    self.current = hit
                elif not rest and self.current is None and self.disks:
                    self.current = next(iter(self.disks))  # ("DF0:" before any disk was asked for: the first one)
                return (self.current if self.current is not None else "\0none"), rest
            if v in self.disks and v != "romorghd":   # (a disk named like the hard disk - "Work:" - is that disk)
                return v, rest
            if v not in _HD_NAMES and self.resolve(v, rest) is not None:
                return v, rest                           # (a disk the script knows by another name than its label)
            return ("hd" if v in _HD_NAMES else v), rest
        return "", path.strip("/")

    def resolve(self, vol: str, rest: str = "", asking: bool = False) -> Optional[Any]:
        """The disk of ours a script means by the volume name ``vol`` when no disk is labelled so (and remember it): the label ignoring
        case / spaces / punctuation, a label that holds or is held by the name, the disk that has the file ``rest`` the script looks
        for, the disk's number (``... 2``, ``... II``), a save disk (none: empty) and - when the script ``asking`` for it - the next disk
        not asked for yet, in order (TOSEC's "Disk k of n" are in the order the original asks): a guess, noted in ``guessed``."""
        key = vol.casefold()
        if key in self.disks:
            return self.disks[key]
        if not key or key in _DRIVES or (key in _HD_NAMES and not asking):
            return None                                   # (asked for as a disk, though named like the hard disk: "Work")
        norm = _norm(vol)
        disks = [d for d in self.order if not isinstance(d, RawDisk)]
        hit = next((d for d in disks if norm and _norm(d.label) == norm), None)
        if hit is None and len(norm) >= 4:
            hit = next((d for d in disks if len(_norm(d.label)) >= 4 and (_norm(d.label) in norm or norm in _norm(d.label))), None)
        if hit is None and rest:
            hit = next((d for d in disks if d not in self.asked and _has(d, rest)), None) or next((d for d in disks if _has(d, rest)), None)
        if hit is None:
            n = _disk_number(vol)
            if n is not None and 1 <= n <= len(self.order):
                hit = self.order[n - 1]
        if hit is None and _SAVE_RE.search(vol):
            hit = MemDisk(vol, {})                       # (a disk for saved games: a blank one)
        if hit is None and len(self.order) == 1:
            hit = self.order[0]                          # (the only disk: nothing else it can be)
        if hit is None and asking:
            hit = next((d for d in self.order if d not in self.asked), None)
            if hit is not None:
                self.guessed.add(key)
        if hit is not None:
            self.disks[key] = hit
        return hit

    def _disk(self, vol: str) -> AdfDisk:
        d = self.disks.get(vol)
        if d is None:
            raise InstallError(f"the disk \"{vol}\" is not among your disks (no ADF with that label)")
        return d

    def _pkg(self, rest: str) -> Optional[Path]:
        p = self.package
        for part in [x for x in rest.split("/") if x]:
            hit = next((c for c in p.iterdir() if c.name.casefold() == part.casefold()), None) if p.is_dir() else None
            if hit is None:
                return None
            p = hit
        return p

    def is_work(self, vol: str) -> bool:
        return vol == "hd"

    def exists(self, path: str) -> int:
        vol, rest = self.split(path)
        if self.is_work(vol):
            k = rest.casefold()
            if k in {x.casefold() for x in self.work} or k in {x.casefold() for x in self.work_dirs} or rest == "":
                return 2 if (k in {x.casefold() for x in self.work_dirs} or rest == "") else 1
            return 0
        if vol == "":
            p = self._pkg(rest)
            return 0 if p is None else (2 if p.is_dir() else 1)
        d = self.disks.get(vol)
        if d is None:
            return 0
        if rest == "":
            return 2
        low = rest.casefold()
        for f, _s in d.files():
            if f.casefold() == low:
                return 1
            if f.casefold().startswith(low + "/"):
                return 2
        return 0

    def listing(self, path: str) -> list[tuple[str, bool]]:
        """The entries directly in a drawer: ``(name, is_dir)``."""
        vol, rest = self.split(path)
        if vol == "":
            p = self._pkg(rest)
            return [] if p is None or not p.is_dir() else [(c.name, c.is_dir()) for c in sorted(p.iterdir())]
        if self.is_work(vol):
            pre = (rest + "/") if rest else ""
            names = {}
            for k in list(self.work) + list(self.work_dirs):
                if k.casefold().startswith(pre.casefold()) and len(k) > len(pre):
                    head = k[len(pre):].split("/")[0]
                    names[head] = names.get(head, False) or "/" in k[len(pre):] or k in self.work_dirs
            return sorted(names.items())
        d = self._disk(vol)
        pre = (rest + "/").casefold() if rest else ""
        names: dict[str, bool] = {}
        for f, _s in d.files():
            if f.casefold().startswith(pre):
                tail = f[len(pre):]
                names[tail.split("/")[0]] = names.get(tail.split("/")[0], False) or "/" in tail
        return sorted(names.items())

    def read(self, path: str) -> bytes:
        vol, rest = self.split(path)
        if vol == "":
            p = self._pkg(rest)
            if p is None or not p.is_file():
                raise InstallError(f"no file {path} in the install package")
            return p.read_bytes()
        if self.is_work(vol):
            for k, v in self.work.items():
                if k.casefold() == rest.casefold():
                    return v
            raise InstallError(f"no file {path}")
        try:
            return self._disk(vol).read(rest)
        except AdfError as exc:
            raise InstallError(f"{path}: {exc}") from None

    def write(self, path: str, data: bytes) -> None:
        vol, rest = self.split(path)
        if not self.is_work(vol):
            raise InstallError(f"cannot write to {path}")
        for k in list(self.work):
            if k.casefold() == rest.casefold():
                del self.work[k]
        self.work[rest] = data
        parts = rest.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            self.work_dirs.add("/".join(parts[:i]))

    def mkdir(self, path: str) -> None:
        vol, rest = self.split(path)
        if self.is_work(vol) and rest:
            parts = rest.split("/")
            for i in range(1, len(parts) + 1):
                self.work_dirs.add("/".join(parts[:i]))

    def delete(self, path: str, everything: bool = False) -> None:
        vol, rest = self.split(path)
        if not self.is_work(vol):
            return
        low = rest.casefold()
        for k in list(self.work):
            if k.casefold() == low or (everything and k.casefold().startswith(low + "/")):
                del self.work[k]
        for k in list(self.work_dirs):
            if k.casefold() == low or (everything and k.casefold().startswith(low + "/")):
                self.work_dirs.discard(k)

    def rename(self, old: str, new: str) -> None:
        data = self.read(old)
        self.delete(old)
        self.write(new, data)


_DRIVES = frozenset({"df0", "df1", "df2", "df3", "rad", "ff0", "pc0", "pc1"})


_SAVE_RE = re.compile(r"\bsav(e|ed|es|egame|ing)?\b|save ?(game|disk)|saved ?games?|gamesave|lotsave", re.IGNORECASE)
_ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7, "viii": 8, "ix": 9, "x": 10, "xi": 11, "xii": 12}


def _norm(x: str) -> str:
    return re.sub(r"[^a-z0-9]", "", x.casefold())


def _disk_number(vol: str) -> Optional[int]:
    """The disk a volume name counts: its trailing number (``NAM_DISK_2``, ``WS1``), a trailing Roman numeral (``ABRA II``)."""
    m = re.search(r"(\d+)\D*$", vol)
    if m:
        return int(m.group(1))
    m = re.search(r"(?:^|[\s_\-#.])(xii|xi|x|ix|viii|vii|vi|v|iv|iii|ii|i)\s*$", vol, re.IGNORECASE)
    return _ROMAN[m.group(1).casefold()] if m else None


def _has(d: AdfDisk, rest: str) -> bool:
    low = rest.casefold()
    return any(f.casefold() == low or f.casefold().startswith(low + "/") for f, _s in d.files())


def _join(a: str, b: str) -> str:
    """``tackon``."""
    if not a:
        return b
    if not b:
        return a
    if ":" in b:
        return b
    return a + ("" if a.endswith((":", "/")) else "/") + b


# --------------------------------------------------------------------------- the Installer language: running

@dataclass
class InstallResult:
    files: dict[str, bytes]
    dirs: set[str]
    log: list[str] = field(default_factory=list)
    version: int = 0
    choices: list[str] = field(default_factory=list)
    guessed: int = 0                                   # disks matched only by the order they were asked for (a guess)


class _Interp:
    def __init__(self, fs: _Fs, app_name: str, version_choice: Optional[int]) -> None:
        self.fs = fs
        self.vars: dict[str, Any] = {"@app-name": app_name, "@default-dest": _HD, "@installer-version": (44 << 16) | 10,
                                     "@language": "english", "@user-level": 1, "@pretend": 0, "@abort-button": "Abort",
                                     "@ioerr": 0, "@error-msg": "", "@special-msg": "", "@execute-dir": "", "@each-name": "",
                                     "@each-type": 0, "@icon": app_name}
        self.procs: dict[str, tuple[list, list]] = {}
        self.version_choice = version_choice
        self.log: list[str] = []
        self.choices: list[str] = []
        self.steps = 0

    # -- evaluation
    def ev(self, x: Any) -> Any:
        if isinstance(x, (int, str)):
            return x
        self.steps += 1
        if self.steps > _MAX_STEPS:
            raise InstallError("the install script runs too long (it loops)")
        if isinstance(x, _Sym):
            n = x.name
            return self.vars.get(n, 0 if n.startswith(("#", "@")) else n)
        if not x:
            return ""
        head = x[0]
        if isinstance(head, str):                           # ("format %s" args): a formatted string
            return self._format(head, [self.ev(a) for a in x[1:]])
        if isinstance(head, list):                          # a block: evaluate each, the last value
            v: Any = ""
            for f in x:
                v = self.ev(f)
            return v
        if isinstance(head, int):
            return head
        name = head.name
        if name.startswith(("#", "@")) and name not in self.procs:     # ((#var) ...): a block; its value is the last one
            v = ""
            for f in x:
                v = self.ev(f)
            return v
        fn = getattr(self, "f_" + name, None) if not name[:1].isalpha() else None       # (=, <, +, - ...)
        if fn is None:
            fn = getattr(self, "f_" + name.replace("-", "_").replace("@", "at_").lower(), None)
        if fn is not None:
            return fn(x[1:])
        if name in self.procs:
            params, body = self.procs[name]
            for p, a in zip(params, x[1:]):
                self.vars[p] = self.ev(a)
            v = ""
            for f in body:
                v = self.ev(f)
            return v
        raise InstallError(f"the install script uses \"{name}\", which this app cannot do")

    @staticmethod
    def _format(fmt: str, args: list) -> str:
        it = iter(args)

        def sub(m: "re.Match[str]") -> str:
            spec = m.group(0)
            if spec == "%%":
                return "%"
            v = next(it, "")
            if spec[-1] in "dx" or spec.endswith(("ld", "lx")):
                try:
                    return str(int(v)) if not spec.endswith(("x", "lx")) else format(int(v), "x")
                except (TypeError, ValueError):
                    return str(v)
            return str(v)
        return re.sub(r"%%|%-?\d*l?[sdxc]", sub, fmt)

    def opts(self, args: list) -> tuple[list, dict[str, list]]:
        """``(plain arguments, {option: [values]})`` of a command: ``(source "x") (dest "y") (all)``."""
        plain, o = [], {}
        for a in args:
            if isinstance(a, list) and a and isinstance(a[0], _Sym) and a[0].name in _OPTIONS:
                o[a[0].name] = [self.ev(v) for v in a[1:]]
            else:
                plain.append(a)
        return plain, o

    def truth(self, v: Any) -> bool:
        return bool(v) and v != "0"

    # -- control
    def f_set(self, a: list) -> Any:
        v: Any = ""
        for i in range(0, len(a) - 1, 2):
            v = self.ev(a[i + 1])
            self.vars[a[i].name] = v
        return v

    f_symbolset = f_set

    def f_procedure(self, a: list) -> Any:
        name = a[0].name
        params = []
        rest = a[1:]
        while rest and isinstance(rest[0], _Sym) and rest[0].name.startswith("#"):
            params.append(rest[0].name)
            rest = rest[1:]
        self.procs[name] = (params, rest)
        return 0

    def f_if(self, a: list) -> Any:
        if self.truth(self.ev(a[0])):
            return self.ev(a[1]) if len(a) > 1 else ""
        return self.ev(a[2]) if len(a) > 2 else ""

    def f_select(self, a: list) -> Any:
        n = self.ev(a[0])
        try:
            return self.ev(a[1 + int(n)])
        except (IndexError, ValueError, TypeError):
            return ""

    def f_while(self, a: list) -> Any:
        guard = 0
        while self.truth(self.ev(a[0])):
            for f in a[1:]:
                self.ev(f)
            guard += 1
            if guard > 100000:
                raise InstallError("the install script loops without end")
        return 0

    def f_until(self, a: list) -> Any:
        guard = 0
        while True:
            for f in a[1:]:
                self.ev(f)
            if self.truth(self.ev(a[0])):
                return 0
            guard += 1
            if guard > 20:                                # (a script waits for a disk to be inserted: it is not among yours)
                raise InstallError("the install waits for a disk that is not among yours")

    def f_trap(self, a: list) -> Any:
        return self.ev(a[1:]) if len(a) > 1 else 0

    def f_onerror(self, a: list) -> Any:
        return 0

    def f_exit(self, a: list) -> Any:
        raise _Exit()

    def f_abort(self, a: list) -> Any:
        raise InstallError("the install script stopped: " + " ".join(str(self.ev(x)) for x in a)[:300])

    # -- values
    def f_cat(self, a: list) -> Any:
        return "".join(str(self.ev(x)) for x in a)

    def f_tackon(self, a: list) -> Any:
        return _join(str(self.ev(a[0])), str(self.ev(a[1])))

    def f_strlen(self, a: list) -> Any:
        return len(str(self.ev(a[0])))

    def f_substr(self, a: list) -> Any:
        s = str(self.ev(a[0]))
        start = int(self.ev(a[1]))
        return s[start:start + int(self.ev(a[2]))] if len(a) > 2 else s[start:]

    def f_pathonly(self, a: list) -> Any:
        s = str(self.ev(a[0]))
        i = max(s.rfind("/"), s.rfind(":"))
        return s[:i + 1] if i >= 0 else ""

    def f_fileonly(self, a: list) -> Any:
        s = str(self.ev(a[0]))
        return s[max(s.rfind("/"), s.rfind(":")) + 1:]

    def f_expandpath(self, a: list) -> Any:
        return str(self.ev(a[0]))

    def f_getenv(self, a: list) -> Any:
        return ""

    def f_getassign(self, a: list) -> Any:
        return ""

    def f_getdevice(self, a: list) -> Any:
        return "DH0"

    def _option(self, a: list) -> Any:                     # (an option where a value is read: its value)
        return self.ev(a[0]) if a else 1

    def f_database(self, a: list) -> Any:
        what = str(self.ev(a[0])).lower()
        return {"cpu": "68020", "graphics-mem": 2 << 20, "total-mem": 8 << 20, "chiprev": "AGA", "vblank": 50}.get(what, "")

    def f_exists(self, a: list) -> Any:
        return self.fs.exists(str(self.ev(a[0])))

    def f_getsize(self, a: list) -> Any:
        try:
            return len(self.fs.read(str(self.ev(a[0]))))
        except InstallError:
            return 0

    def f_getversion(self, a: list) -> Any:
        return 0

    def f_iconinfo(self, a: list) -> Any:
        return 0

    def f_earlier(self, a: list) -> Any:
        return 0

    def _num(self, v: Any) -> int:
        try:
            return int(v)
        except (TypeError, ValueError):
            return 0

    def f_bitand(self, a: list) -> Any:
        return self._num(self.ev(a[0])) & self._num(self.ev(a[1]))

    def f_bitor(self, a: list) -> Any:
        return self._num(self.ev(a[0])) | self._num(self.ev(a[1]))

    def f_in(self, a: list) -> Any:
        v = self._num(self.ev(a[0]))
        return int(any(v & (1 << self._num(self.ev(b))) for b in a[1:]))

    def _cmp(self, a: list, op: Callable[[Any, Any], bool]) -> int:
        x, y = self.ev(a[0]), self.ev(a[1])
        if isinstance(x, int) and isinstance(y, int):
            return int(op(x, y))
        if isinstance(x, int) or isinstance(y, int):
            try:
                return int(op(int(x), int(y)))
            except (TypeError, ValueError):
                pass
        return int(op(str(x), str(y)))

    def f_at_eq(self, a): return 0

    def ev_op(self, name: str, a: list) -> Any:
        return 0

    def f_and(self, a: list) -> Any:
        return int(all(self.truth(self.ev(x)) for x in a))

    def f_or(self, a: list) -> Any:
        return int(any(self.truth(self.ev(x)) for x in a))

    def f_not(self, a: list) -> Any:
        return int(not self.truth(self.ev(a[0])))

    # -- the user: default answers
    def f_askdir(self, a: list) -> Any:
        _p, o = self.opts(a)
        return _HD

    def f_askfile(self, a: list) -> Any:
        _p, o = self.opts(a)
        return (o.get("default") or [""])[0]

    def f_askstring(self, a: list) -> Any:
        _p, o = self.opts(a)
        return (o.get("default") or [""])[0]

    def f_asknumber(self, a: list) -> Any:
        _p, o = self.opts(a)
        return (o.get("default") or [0])[0]

    def f_askbool(self, a: list) -> Any:
        _p, o = self.opts(a)
        if re.search(r"\brun\b.*\bnow\b", str((o.get("prompt") or [""])[0]), re.IGNORECASE):
            return 0                                      # ("Run Monkey Island 2 now?": not here)
        return (o.get("default") or [1])[0]

    def f_askchoice(self, a: list) -> Any:
        _p, o = self.opts(a)
        choices = [str(c) for c in o.get("choices", [])]
        prompt = str((o.get("prompt") or [""])[0]).lower()
        if self.version_choice is not None and "version" in prompt:
            self.choices = choices
            return self.version_choice
        return (o.get("default") or [0])[0]

    def f_askoptions(self, a: list) -> Any:
        _p, o = self.opts(a)
        return (o.get("default") or [0])[0]

    def f_askdisk(self, a: list) -> Any:
        _p, o = self.opts(a)
        vol = str((o.get("dest") or [""])[0]).rstrip(":")
        if vol.casefold() not in self.fs.disks:          # (asked for a floppy: never the hard disk, though one is named "Work")
            alias = self.fs.resolve(vol, "", asking=True)   # (a disk a cracker gave another name: by its label, number or order)
            if alias is None:
                raise InstallError(f"the install asks for the disk \"{vol}\": no ADF of yours has that label")
        disk = self.fs.disks.get(vol.casefold())
        if disk is not None and not any(disk is d for d in self.fs.asked):
            self.fs.asked.append(disk)
        if vol.casefold() in self.fs.disks:
            self.fs.current = vol.casefold()              # (the disk asked for is the one in the drive now: "DF0:" is it)
        self.log.append(f"disk {vol}")
        return 1

    def _by_number(self, vol: str) -> Optional[AdfDisk]:
        """The disk a volume name means when no disk has that label: its number (``NAM_DISK_2``, ``WS1``, ``A10 #1``), or the only disk."""
        order = [d for d in self.fs.order if isinstance(d, AdfDisk)]
        m = re.search(r"(\d+)\D*$", vol)
        if m:
            n = int(m.group(1))
            return order[n - 1] if 1 <= n <= len(order) else None
        return order[0] if len(order) == 1 else None

    def _noop(self, a: list) -> Any:
        return 0

    f_message = f_working = f_welcome = f_complete = f_debug = f_user = f_openwbobject = f_showwbobject = _noop
    f_closewbobject = f_startup = f_textfile = f_protect = f_makeassign = f_transcript = f_retrace = _noop

    def f_tooltype(self, a: list) -> Any:
        """``(tooltype (dest "x") (settooltype "Slave" "x.Slave") (setdefaulttool "WHDLoad") (setstack n) (noposition))``: the icon
        ``x.info`` changed as the script says - WHDLoad (and PUAE) find the slave by the icon's ``Slave=``."""
        dest, ops, tool, stack, nopos = "", [], None, None, False
        for x in a:                                       # (in their order: a tooltype can be removed, then set again)
            if not (isinstance(x, list) and x and isinstance(x[0], _Sym)):
                continue
            n, vals = x[0].name, [self.ev(v) for v in x[1:]]
            if n == "dest" and vals:
                dest = str(vals[0])
            elif n == "settooltype" and vals:
                ops.append((str(vals[0]), str(vals[1]) if len(vals) > 1 else None))
            elif n == "setdefaulttool":
                tool = str(vals[0]) if vals else ""
            elif n == "setstack" and vals:
                stack = self._num(vals[0])
            elif n == "noposition":
                nopos = True
        path = dest + ".info"
        vol, _rest = self.fs.split(path)
        if not dest or not self.fs.is_work(vol) or self.fs.exists(path) != 1:
            self.log.append(f"tooltype: no icon {path}")
            return 0
        try:
            self.fs.write(path, edit_icon(self.fs.read(path), ops, tool, stack, nopos))
        except ValueError as exc:
            self.log.append(f"tooltype: {path}: {exc}")
            return 0
        return 1

    # -- files
    def f_makedir(self, a: list) -> Any:
        self.fs.mkdir(str(self.ev(a[0])))
        return 1

    def f_delete(self, a: list) -> Any:
        plain, o = self.opts(a)
        self.fs.delete(str(self.ev(plain[0])), everything=True)
        return 1

    def f_rename(self, a: list) -> Any:
        old, new = str(self.ev(a[0])), str(self.ev(a[1]))
        if self.fs.exists(old) == 1:
            self.fs.rename(old, new)
        return 1

    def f_copylib(self, a: list) -> Any:
        return self.f_copyfiles(a)

    def f_copyfiles(self, a: list) -> Any:
        _p, o = self.opts(a)
        src = str((o.get("source") or [""])[0])
        dest = str((o.get("dest") or [""])[0])
        newname = (o.get("newname") or [None])[0]
        kind = self.fs.exists(src)
        if kind == 0:
            if "optional" in o or "nofail" in o or self.fs.split(src)[0] == "":
                self.log.append(f"skipped {src} (not in the package)")
                return 0                                  # (an icon of a kind the package does not have)
            raw = next((d for d in self.fs.disks.values() if isinstance(d, RawDisk)), None)
            why = f" ({raw.reason}: not an AmigaDOS disk)" if raw is not None else ""
            raise InstallError(f"the install copies {src}, which is not there{why}")
        if kind == 1:
            name = newname or re.split(r"[:/]", src)[-1]
            self.fs.write(_join(dest, str(name)), self.fs.read(src))
            return 1
        rx = None
        if "pattern" in o:
            rx = pattern_regex(str(o["pattern"][0]))
        elif "choices" in o:
            wanted = {str(c).casefold() for c in o["choices"]}
            rx = re.compile("|".join(re.escape(w) for w in wanted) or "(?!)", re.IGNORECASE)
        elif "all" not in o:
            rx = re.compile(r".*")
        only_files = "files" in o
        n = 0
        for name, is_dir in self.fs.listing(src):
            if rx is not None and not rx.fullmatch(name):
                continue
            if is_dir:
                if not only_files:
                    n += self._copy_tree(_join(src, name), _join(dest, name))
            else:
                self.fs.write(_join(dest, name), self.fs.read(_join(src, name)))
                n += 1
        self.log.append(f"copied {n} from {src}")
        return 1

    def _copy_tree(self, src: str, dest: str) -> int:
        (sv, sr), (dv, dr) = self.fs.split(src), self.fs.split(dest)
        if self.fs.is_work(sv) and self.fs.is_work(dv) and (dr.casefold() + "/").startswith(sr.casefold() + "/"):
            raise InstallError(f"the install copies {src} into itself")   # (it would never end)
        self.fs.mkdir(dest)
        n = 0
        for name, is_dir in list(self.fs.listing(src)):
            if is_dir:
                n += self._copy_tree(_join(src, name), _join(dest, name))
            else:
                self.fs.write(_join(dest, name), self.fs.read(_join(src, name)))
                n += 1
        return n

    def f_run(self, a: list) -> Any:
        plain, _o = self.opts(a)
        cmd = str(self.ev(plain[0])) if plain else ""
        return self._shell(cmd)

    f_execute = f_run

    def _shell(self, script: str) -> int:
        cwd = ""
        for line in script.split("\n"):
            parts = _split_args(line.strip())
            if not parts:
                continue
            while parts and (parts[0].casefold() == "run" or parts[0].startswith(">")):
                parts = parts[1:]                         # ("run >NIL: WHDLoad ...": the command after them)
            if not parts:
                continue
            word = parts[0].casefold().split("/")[-1].split(":")[-1]
            args = parts[1:]
            if word == "cd":
                cwd = args[0] if args else cwd
            elif word in ("which", "setenv", "copy", "multiview", "more", "assign", "wait", "echo", "avail", "version", "resident",
                          "whdload", "diskimagectrl", "diskchange", "opendrawer", "filenote", "protect", "makedir", "info", "list",
                          "type", "relabel", "setdate"):
                if word == "makedir":
                    for x in args:
                        self.fs.mkdir(_join(cwd, x))
                continue
            elif word == "rename":
                names = [x for x in args if x.casefold() not in ("quiet", "as", "to")]
                if len(names) >= 2 and self.fs.exists(_join(cwd, names[0])) == 1:
                    self.fs.rename(_join(cwd, names[0]), _join(cwd, names[1]))
            elif word in ("lhx", "lhx.exe", "lha", "lhex") and args and args[0].casefold() in ("x", "e", "-x", "-e"):
                self._unlha(cwd, [x for x in args[1:] if not x.startswith(">")])
                continue                                        # (tools there are, an environment: nothing to do here)
            elif word == "delete":
                for x in args:
                    if x.casefold() not in ("all", "quiet", "force"):
                        self.fs.delete(_join(cwd, x), everything=True)
            elif word == "dic":
                self._dic(cwd, args)
            elif word == "rawdic":
                self._rawdic(cwd, args)
            elif word == "patcher":
                raise InstallError(f"this install reads the disks with {parts[0]} (the original disks' own format): not done here")
            elif word in ("xfddecrunch", "xfddecrunchall", "unpack"):
                raise InstallError(f"this install needs {parts[0]} to unpack a file (an Amiga program)")
            else:
                raise InstallError(f"the install script runs \"{line.strip()[:80]}\" (an Amiga program)")
        return 0

    def _unlha(self, cwd: str, args: list[str]) -> None:
        """``lha x archive [dest]``: an LhA archive the install unpacks, with this app's reader."""
        from .lha import LhaError, LhaFile
        if not args:
            raise InstallError("lha: no archive")
        src = _join(cwd, args[0])
        dest = _join(cwd, args[1]) if len(args) > 1 else cwd
        try:
            arc = LhaFile(self.fs.read(src))
            for e in arc.entries:
                if not e.is_dir:
                    self.fs.write(_join(dest, e.name), arc.read(e))
        except LhaError as exc:
            raise InstallError(f"lha: {exc}") from None

    def _rawdic(self, cwd: str, args: list[str]) -> None:
        """RawDIC: the install's imager (``SLAVE=x.islave``) run on the disks' tracks (disk 1, 2 ... in the order the set has them);
        the files it writes go in the current drawer."""
        opt: dict[str, str] = {}
        plain = []
        for x in args:
            k, eq, v = x.partition("=")
            if eq:
                opt[k.casefold()] = v
            else:
                plain.append(x.casefold())
        slave = opt.get("slave") or next((x for x in plain if x.endswith(".islave")), "")
        if not slave:
            raise InstallError("RawDIC: no imager (SLAVE=) named")
        path = _join(cwd, slave)
        if self.fs.exists(path) != 1:
            raise InstallError(f"RawDIC: the imager {slave} is not there")
        disks = {i + 1: d.tracks for i, d in enumerate(self.fs.order) if getattr(d, "tracks", None) is not None}
        try:
            im = rawdic.Imager(self.fs.read(path))
            here = lambda name: self.fs.read(_join(cwd, name)) if self.fs.exists(_join(cwd, name)) == 1 else None
            files = im.run(disks, ignore_errors="ignoreerrors" in plain, read_file=here)
        except rawdic.ImagerError as exc:
            raise InstallError(f"RawDIC ({slave}): {exc}") from None
        for d in im.made_dirs:
            self.fs.mkdir(_join(cwd, d))
        for name, data in files.items():
            self.fs.write(_join(cwd, name), data)
        self.log.append(f"RawDIC {slave}: {', '.join(files)}")

    def _dic(self, cwd: str, args: list[str]) -> None:
        """DIC: an image of whole disks (``FD=n LD=m``: the n-th to m-th disk, which DIC does not check), ``Disk.<n>``: the ADF itself."""
        opt = {k.casefold(): v for k, v in (x.split("=", 1) for x in args if "=" in x)}
        try:
            first = int(opt.get("fd", "1") or 1)
            last = int(opt.get("ld", str(first)) or first)
            v = opt.get("size", "").strip()
            size = int(v[1:], 16) if v.startswith("$") else int(v, 0) if v else 0      # (SIZE=901120, SIZE=$b2c00)
        except ValueError:
            raise InstallError("DIC: arguments it does not understand") from None
        for n in range(first, last + 1):
            if not 1 <= n <= len(self.fs.order):
                raise InstallError(f"DIC: the install images disk {n}, this version has {len(self.fs.order)}")
            data = self.fs.order[n - 1].data
            if not data:
                if getattr(self.fs.order[n - 1], "tracks", None) is not None:
                    raise InstallError(f"DIC: disk {n} is in the game's own format (DIC reads standard tracks only)")
                raise InstallError("DIC: that disk is no floppy image")
            img = bytearray(data[:size] if size else data)
            for part in opt.get("skiptrack", "").split(","):        # (SKIPTRACK=100-101: tracks DIC leaves empty)
                lo, _, hi = part.partition("-")
                if lo.strip().isdigit():
                    for t in range(int(lo), int(hi or lo) + 1):
                        img[t * 0x1600:(t + 1) * 0x1600] = bytes(len(img[t * 0x1600:(t + 1) * 0x1600]))
            self.fs.write(_join(cwd, f"Disk.{n}"), bytes(img))


_MAX_STEPS = 2_000_000          # (an install evaluates some thousand forms: a script that goes on and on loops)

_OPTIONS = {"source", "dest", "pattern", "all", "choices", "newname", "help", "prompt", "default", "confirm", "infos", "noposition",
            "nogauge", "files", "fonts", "optional", "nofail", "force", "askuser", "safe", "settooltype", "setdefaulttool", "setstack",
            "swapcolors", "disk", "assigns", "back", "range", "newpath", "include", "command", "append"}


def _split_args(line: str) -> list[str]:
    out, cur, q = [], "", False
    for c in line:
        if c == '"':
            q = not q
        elif c in " \t" and not q:
            if cur:
                out.append(cur)
                cur = ""
        else:
            cur += c
    if cur:
        out.append(cur)
    return out


# comparisons and arithmetic have names that are no Python identifiers
def _install_ops() -> None:
    import operator
    ops = {"=": operator.eq, "<>": operator.ne, "<": operator.lt, ">": operator.gt, "<=": operator.le, ">=": operator.ge}
    for sym, fn in ops.items():
        setattr(_Interp, "f_" + sym, lambda self, a, _f=fn: self._cmp(a, _f))
    def arith(fn: Callable[[int, int], int]) -> Callable[[Any, list], int]:
        def run(self: _Interp, a: list) -> int:
            vals = [self._num(self.ev(x)) for x in a]
            out = vals[0] if vals else 0
            for v in vals[1:]:
                out = fn(out, v)
            return out
        return run
    setattr(_Interp, "f_+", arith(lambda x, y: x + y))
    setattr(_Interp, "f_-", arith(lambda x, y: x - y))
    setattr(_Interp, "f_*", arith(lambda x, y: x * y))
    setattr(_Interp, "f_/", arith(lambda x, y: x // y if y else 0))


_install_ops()
for _o in ("default", "help", "prompt", "choices", "pattern", "newname", "source", "dest", "infos", "noposition", "confirm", "all",
           "files", "fonts", "optional", "nofail", "noreq", "safe", "settooltype", "setdefaulttool", "setstack", "command", "append",
           "include", "back", "range", "disk", "assigns", "newpath", "askuser", "force", "swapcolors", "nogauge"):
    if not hasattr(_Interp, "f_" + _o):
        setattr(_Interp, "f_" + _o, _Interp._option)


def _app_name(script: Path) -> str:
    """The ``APPNAME`` tooltype of the script's icon (what the Installer calls ``@app-name``), or ""."""
    icon = script.with_name(script.name + ".info")
    try:
        m = re.search(rb"APPNAME=([^\x00\r\n]+)", icon.read_bytes(), re.IGNORECASE)
    except OSError:
        return ""
    return m.group(1).decode("latin-1").strip() if m else ""


def version_choices(script_text: str) -> list[int]:
    """The answers to the script's "which version" question that name a version (empty: the script does not ask)."""
    return sorted(version_names(script_text))


def version_names(script_text: str) -> dict[int, str]:
    """``{answer: its text}`` of the script's "which version" question (``"AGA version (5 disks)"``)."""
    m = re.search(r'\(set\s+#multiple-versions\s+"([^"]*)"\)', script_text, re.IGNORECASE)
    if not m or not m.group(1):
        return {}
    i = script_text.lower().find("(choices", script_text.lower().find("(procedure p_selectversion"))
    if i < 0:
        return {}
    names, j, cur = [], i + len("(choices"), None
    while j < len(script_text):                          # (to its closing bracket; "ECS version (4 disks)" has one of its own)
        c = script_text[j]
        if cur is not None:
            if c == '"':
                names.append(cur)
                cur = None
            else:
                cur += c
        elif c == '"':
            cur = ""
        elif c == ";":
            j = script_text.find("\n", j)
            if j < 0:
                break
        elif c == ")":
            break
        j += 1
    return {n: c.strip() for n, c in enumerate(names) if c.strip()}


class Package:
    """An install package unpacked once (a temporary folder) to install from several sets of disks::

        with Package(lha) as pkg:
            result = pkg.install(disks)
    """

    def __init__(self, lha: bytes) -> None:
        import tempfile
        from .lha import LhaFile
        arc = LhaFile(lha)
        self.work = Path(tempfile.mkdtemp(prefix="romorg-whd-"))
        try:
            for e in arc.entries:
                if e.is_dir:
                    continue
                parts = [p for p in e.name.replace("\\", "/").split("/") if p not in ("", ".", "..")]
                if not parts:
                    continue
                dest = self.work.joinpath(*parts)
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(arc.read(e))
            self.dir = next((Path(r) for r, _d, fs in os.walk(self.work) for f in fs
                             if f.casefold().startswith("install") and not f.casefold().endswith(".info")), None)
            if self.dir is None:
                raise InstallError("the install has no Install script")
            script = next(p for p in self.dir.iterdir() if p.name.casefold().startswith("install") and not p.name.casefold().endswith(".info"))
            self.names = version_names(script.read_text(encoding="latin-1"))
        except BaseException:
            self.close()
            raise

    def install(self, disks: list[Any], volumes: Optional[list[MemDisk]] = None, cd: Optional[bool] = None) -> InstallResult:
        """Each version the script names is tried; the first that installs wins (else the first reason it gave). The script's CD
        versions ("CD³² release") only for a CD's files (``cd``; by default: when ``volumes`` are given) - floppies made into the CD
        version would install a game that is not theirs."""
        first: Optional[InstallError] = None
        cd = bool(volumes) if cd is None else cd
        for v in self.tries(len(disks)):
            if v is not None and not cd and _CD_RE.search(self.names.get(v, "")):
                continue
            try:
                return install(self.dir, disks, v, volumes)
            except InstallError as exc:
                first = first or exc
        raise first  # type: ignore[misc]

    def tries(self, n_disks: int) -> list[Optional[int]]:
        """The versions to try, in order: those whose text names this many disks ("AGA version (5 disks)": the version these disks
        are, where several would install), the script's own default, then the others."""
        fits = [v for v, t in self.names.items() if re.search(rf"\b{n_disks}\s*(\+\s*\d+\s*)?disks?\b", t, re.IGNORECASE)
                and not re.search(rf"\b{n_disks}\s*\+", t)]
        return fits + [None] + [v for v in sorted(self.names) if v not in fits]

    def close(self) -> None:
        import shutil
        shutil.rmtree(self.work, ignore_errors=True)

    def __enter__(self) -> "Package":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def install_archive(lha: bytes, disks: list[Any], volumes: Optional[list[MemDisk]] = None) -> InstallResult:
    """Run the install in ``lha`` (an install package as whdload.de has it) on ``disks`` (paths or ADF bytes)."""
    with Package(lha) as pkg:
        return pkg.install(disks, volumes)


def install(package: Path, disks: list[Any], version: Optional[int] = None, volumes: Optional[list[MemDisk]] = None) -> InstallResult:
    """Run the install script of ``package`` (the unpacked install's drawer, holding ``Install``) with the ADFs ``disks``."""
    package = Path(package)
    script = next((p for p in package.iterdir() if p.name.casefold() in ("install", "install-" + package.name.casefold())
                   or (p.name.casefold().startswith("install") and p.is_file() and not p.name.casefold().endswith(".info"))), None)
    if script is None:
        raise InstallError("the install has no Install script")
    app = _app_name(script)
    if not app:
        slave = next((p for p in sorted(package.iterdir()) if p.suffix.casefold() == ".slave"), None)
        app = slave.stem if slave is not None else package.name
    adfs = [floppy(d, n) for n, d in enumerate(disks)]
    fs = _Fs(package, adfs + list(volumes or ()))
    it = _Interp(fs, app, version)
    forms = _parse(script.read_text(encoding="latin-1"))
    try:
        for f in forms:
            it.ev(f)
    except _Exit:
        pass
    except AdfError as exc:                                 # (a damaged disk, found while the script reads it)
        raise InstallError(f"a disk cannot be read: {exc}") from None
    if not any(k.casefold().endswith(".slave") for k in fs.work):
        raise InstallError("the install script made no game (no .slave was installed)")
    return InstallResult(files=dict(fs.work), dirs=set(fs.work_dirs), log=it.log, version=int(it.vars.get("#version", 0) or 0),
                         choices=it.choices, guessed=len(fs.guessed))


# --------------------------------------------------------------------------- icons

_NO_POSITION = 0x80000000


def icon_info(data: bytes) -> tuple[Optional[str], list[str], int, int]:
    """``(default tool, tooltypes, where they start, where they end)`` of an Amiga icon (a ``.info``: a DiskObject)."""
    if len(data) < 78 or data[:2] != b"\xe3\x10":
        raise ValueError("not an Amiga icon")
    def u32(off: int) -> int:
        if off + 4 > len(data):
            raise ValueError("a damaged icon")
        return struct.unpack_from(">I", data, off)[0]
    off = 78 + (56 if u32(66) else 0)                     # (a drawer's window)
    for render in (u32(22), u32(26)):                    # (the image, and the selected image)
        if render:
            w, h, depth = struct.unpack_from(">hhh", data, off + 4)
            off += 20 + (((w + 15) // 16) * 2 * h * depth if u32(off + 10) else 0)
    start = off
    tool = None
    if u32(50):
        n = u32(off)
        tool = data[off + 4:off + 4 + n].rstrip(b"\0").decode("latin-1")
        off += 4 + n
    tts: list[str] = []
    if u32(54):
        count = u32(off) // 4 - 1
        off += 4
        for _ in range(max(count, 0)):
            n = u32(off)
            tts.append(data[off + 4:off + 4 + n].rstrip(b"\0").decode("latin-1"))
            off += 4 + n
    if off > len(data):
        raise ValueError("a damaged icon")
    return tool, tts, start, off


def edit_icon(data: bytes, ops: list[tuple[str, Optional[str]]], tool: Optional[str] = None, stack: Optional[int] = None,
              noposition: bool = False) -> bytes:
    """The icon with its tooltypes set (``(name, value)``; value None removes it, "" leaves the bare name), as the Installer's
    ``tooltype`` does; ``tool``: its default tool."""
    old_tool, tts, start, end = icon_info(data)
    for name, value in ops:
        low = name.casefold()
        at = next((i for i, t in enumerate(tts) if t.split("=", 1)[0].strip().casefold() == low), None)
        if value is None:
            if at is not None:
                del tts[at]
            continue
        entry = name if value == "" else f"{name}={value}"
        if at is None:
            tts.append(entry)
        else:
            tts[at] = entry
    tool = old_tool if tool is None else tool
    def text(t: str) -> bytes:
        b = t.encode("latin-1", "replace") + b"\0"
        return struct.pack(">I", len(b)) + b
    body = (text(tool) if tool is not None else b"")
    body += struct.pack(">I", (len(tts) + 1) * 4) + b"".join(text(t) for t in tts) if tts else b""
    head = bytearray(data[:start])
    struct.pack_into(">I", head, 50, 1 if tool is not None else 0)       # (pointers on the Amiga: here only "there is one")
    struct.pack_into(">I", head, 54, 1 if tts else 0)
    if stack is not None:
        struct.pack_into(">I", head, 74, stack)
    if noposition:
        struct.pack_into(">II", head, 58, _NO_POSITION, _NO_POSITION)
    return bytes(head) + body + data[end:]


# --------------------------------------------------------------------------- the .lha

def _dos_time(t: float) -> int:
    lt = time.localtime(t)
    return ((max(lt.tm_year, 1980) - 1980) << 25) | (lt.tm_mon << 21) | (lt.tm_mday << 16) | (lt.tm_hour << 11) | (lt.tm_min << 5) | (lt.tm_sec // 2)


def write_lha(path: Path, files: dict[str, bytes], dirs: Optional[set[str]] = None, when: Optional[float] = None) -> None:
    """An LhA archive (header level 1, stored data) of ``files`` ("Drawer/name" -> bytes), as WHDLoad archives are laid out."""
    stamp = _dos_time(when if when is not None else time.time())
    out = bytearray()
    for name in sorted(files, key=lambda n: n.casefold()):
        data = files[name]
        d, _, fname = name.rpartition("/")
        nm = fname.encode("latin-1", "replace")
        ext = b""
        if d:
            dir_data = d.replace("/", "\xff").encode("latin-1", "replace") + b"\xff"
            ext = bytes([2]) + dir_data + b"\x00\x00"          # (directory name; no further header)
        packed = len(ext) + len(data)
        body = (b"-lh0-" + struct.pack("<III", packed, len(data), stamp) + bytes([0x20, 1, len(nm)]) + nm
                + struct.pack("<H", crc16(data)) + b"A" + struct.pack("<H", len(ext)))
        out += bytes([len(body), sum(body) & 0xFF]) + body + ext + data
    for d in sorted(dirs or (), key=lambda n: n.casefold()):          # (a drawer the install made and left empty: a save drawer)
        low = d.casefold().rstrip("/") + "/"
        if not d or any(k.casefold().startswith(low) for k in files) or any(x.casefold().startswith(low) for x in (dirs or ()) if x != d):
            continue
        ext = bytes([2]) + d.strip("/").replace("/", "\xff").encode("latin-1", "replace") + b"\xff" + b"\x00\x00"
        body = (b"-lhd-" + struct.pack("<III", len(ext), 0, stamp) + bytes([0x10, 1, 0]) + struct.pack("<H", 0) + b"A"
                + struct.pack("<H", len(ext)))
        out += bytes([len(body), sum(body) & 0xFF]) + body + ext
    out += b"\x00"
    Path(path).write_bytes(bytes(out))
