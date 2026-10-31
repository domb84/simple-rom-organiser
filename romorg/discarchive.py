"""A disc image inside a ``.zip`` or ``.7z``: list it, read one member as a stream, take the files out.

A zipped disc (an ``.iso``, or a ``.cue`` / ``.gdi`` with its tracks) is identified without being unpacked: each track is streamed
through the hashes (a zip says each member's CRC-32 in its directory, which rules most games out for free). Converting it to a CHD
takes the files out to a work folder first. Zip uses :mod:`zipfile`; a ``.7z`` uses :mod:`romorg.sevenzip` (LZMA / LZMA2 / stored,
no 7-Zip needed) and, for what that cannot read, the real ``7z`` (``7z e -so`` streams a member).
"""

from __future__ import annotations

import contextlib
import hashlib
import lzma
import os
import subprocess
import zipfile
import zlib
from pathlib import Path
from typing import Any, BinaryIO, Callable, Iterator, Optional

from . import scanner, sevenzip

__all__ = ["EXTS", "DISC_EXTS", "Member", "list_members", "open_member", "iter_files", "hash_member", "hash_members", "extract_members", "is_disc_archive", "ArchiveError"]

EXTS = (".zip", ".7z")
DISC_EXTS = (".iso", ".cue", ".gdi")
CHUNK = 1 << 20


class ArchiveError(Exception):
    """The archive cannot be read (damaged, encrypted, or a 7z that needs 7-Zip when there is none)."""


class Member:
    __slots__ = ("name", "size", "crc")

    def __init__(self, name: str, size: int, crc: str) -> None:
        self.name, self.size, self.crc = name, size, crc

    def __repr__(self) -> str:
        return f"Member({self.name!r}, {self.size}, {self.crc})"


def list_members(path: Path) -> list[Member]:
    """The files of an archive (folders left out), in the archive's order. Raises :class:`ArchiveError`."""
    path = Path(path)
    try:
        if path.suffix.lower() == ".zip":
            with zipfile.ZipFile(path) as zf:
                return [Member(i.filename, i.file_size, f"{i.CRC & 0xFFFFFFFF:08x}") for i in zf.infolist() if not i.is_dir()]
        try:
            return [Member(n, s, (c or "").lower()) for n, s, c in sevenzip.Archive(path).listing()]
        except (sevenzip.Unsupported, ValueError, OSError):
            exe = scanner.find_7z()
            if exe is None:
                raise ArchiveError("this 7z archive needs 7-Zip to be read") from None
            return [Member(n, s, (c or "").lower()) for n, s, c in scanner.list_7z(path, exe)]
    except (zipfile.BadZipFile, zipfile.LargeZipFile, RuntimeError, EOFError, zlib.error, NotImplementedError) as exc:
        raise ArchiveError(str(exc) or type(exc).__name__) from exc


def is_disc_archive(members: list[Member]) -> bool:
    """True when the archive holds something a disc is made of: an ``.iso``, a ``.cue`` or a ``.gdi``."""
    return any(Path(m.name).suffix.lower() in DISC_EXTS for m in members)


@contextlib.contextmanager
def open_member(path: Path, name: str) -> Iterator[BinaryIO]:
    """One member as a stream (``read(n)``); nothing is written to disk."""
    path = Path(path)
    if path.suffix.lower() == ".zip":
        try:
            with zipfile.ZipFile(path) as zf, zf.open(name) as f:
                yield f  # type: ignore[misc]
        except (zipfile.BadZipFile, RuntimeError, NotImplementedError, zlib.error, EOFError) as exc:
            raise ArchiveError(f"{name}: {exc}") from exc
        return
    try:
        reader = sevenzip.MemberReader(sevenzip.Archive(path), name)
    except (sevenzip.Unsupported, ValueError, OSError):
        reader = None
    if reader is not None:
        try:
            yield reader  # type: ignore[misc]
        finally:
            reader.close()
        return
    exe = scanner.find_7z()
    if exe is None:
        raise ArchiveError("this 7z archive needs 7-Zip to be read")
    proc = subprocess.Popen([exe, "e", "-so", "-p", "--", str(path), name], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL)
    try:
        assert proc.stdout is not None
        yield proc.stdout  # type: ignore[misc]
    finally:
        with contextlib.suppress(OSError):
            proc.kill()
        proc.wait()


def iter_files(path: Path, members: list[Member]) -> Iterator[tuple[Member, bytes]]:
    """``(member, bytes)`` pieces of several members, read in one pass: a ``.7z``'s solid block is decoded once for all of them (not once
    per member, as :func:`open_member` does), a zip's members one after the other. Raises :class:`ArchiveError`."""
    path = Path(path)
    by_name = {m.name: m for m in members}
    if path.suffix.lower() == ".7z":
        try:
            arc = sevenzip.Archive(path)
            for name, piece in arc.iter_files(list(by_name)):
                yield by_name[name], piece
            return
        except sevenzip.Unsupported:
            pass                                          # (a method the reader does not do: one member at a time through 7-Zip)
        except (ValueError, OSError, KeyError, lzma.LZMAError, EOFError) as exc:
            raise ArchiveError(f"cannot read {path.name}: {exc}") from exc
    for m in members:
        with open_member(path, m.name) as f:
            got = False
            for block in iter(lambda: f.read(CHUNK), b""):
                got = True
                yield m, block
            if not got:
                yield m, b""


def hash_members(path: Path, members: list[Member], progress: Optional[Callable[[int], None]] = None,
                 cancel: Optional[Callable[[], bool]] = None) -> dict[str, tuple[str, str]]:
    """``{member name: (crc32, sha1)}`` of several members in one pass (:func:`iter_files`). Raises :class:`ArchiveError` when one is
    shorter or longer than the archive says."""
    state: dict[str, list] = {m.name: [0, hashlib.sha1(), 0] for m in members}
    for m, block in iter_files(path, members):
        if cancel is not None and cancel():
            raise scanner.ScanCancelled()
        st = state[m.name]
        st[0] = zlib.crc32(block, st[0])
        st[1].update(block)
        st[2] += len(block)
        if progress is not None and block:
            progress(len(block))
    for m in members:
        if state[m.name][2] != m.size:
            raise ArchiveError(f"{m.name} is {state[m.name][2]} bytes in {Path(path).name} (the archive says {m.size})")
    return {n: (f"{st[0] & 0xFFFFFFFF:08x}", st[1].hexdigest()) for n, st in state.items()}


def hash_member(path: Path, member: Member, progress: Optional[Callable[[int], None]] = None,
                cancel: Optional[Callable[[], bool]] = None) -> tuple[str, str]:
    """``(crc32, sha1)`` of a member, streamed. Raises :class:`ArchiveError` when it is shorter or longer than the archive says."""
    crc, sha1, n = 0, hashlib.sha1(), 0
    with open_member(path, member.name) as f:
        for block in iter(lambda: f.read(CHUNK), b""):
            if cancel is not None and cancel():
                raise scanner.ScanCancelled()
            crc = zlib.crc32(block, crc)
            sha1.update(block)
            n += len(block)
            if progress is not None:
                progress(len(block))
    if n != member.size:
        raise ArchiveError(f"{member.name} is {n} bytes in {Path(path).name} (the archive says {member.size})")
    return f"{crc & 0xFFFFFFFF:08x}", sha1.hexdigest()


def extract_members(path: Path, members: list[Member], dest: Path, progress: Optional[Callable[[int], None]] = None,
                    cancel: Optional[Callable[[], bool]] = None) -> dict[str, Path]:
    """Write the members into ``dest`` (their paths below it, never outside it); ``{member name: file}``. Every file is checked against
    the CRC-32 the archive lists. One pass over the archive (:func:`iter_files`)."""
    dest = Path(dest)
    out: dict[str, Path] = {}
    root = os.path.realpath(dest)
    targets: dict[str, Path] = {}
    for m in members:
        target = Path(os.path.realpath(dest / m.name.replace("\\", "/")))
        if os.path.commonpath([root, str(target)]) != root:
            raise ArchiveError(f"{m.name} would be written outside the work folder")
        targets[m.name] = target
    sums: dict[str, list[int]] = {m.name: [0, 0] for m in members}
    current: Optional[str] = None
    dst: Optional[BinaryIO] = None
    try:
        for m, block in iter_files(path, members):
            if cancel is not None and cancel():
                raise scanner.ScanCancelled()
            if m.name != current:
                if dst is not None:
                    dst.close()
                targets[m.name].parent.mkdir(parents=True, exist_ok=True)
                dst = open(targets[m.name], "wb")
                current = m.name
            dst.write(block)
            s = sums[m.name]
            s[0] = zlib.crc32(block, s[0])
            s[1] += len(block)
            if progress is not None and block:
                progress(len(block))
    finally:
        if dst is not None:
            dst.close()
    for m in members:
        crc, n = sums[m.name]
        if not targets[m.name].exists() and m.size == 0:
            targets[m.name].parent.mkdir(parents=True, exist_ok=True)
            targets[m.name].write_bytes(b"")
        if n != m.size or (m.crc and f"{crc & 0xFFFFFFFF:08x}" != m.crc):
            raise ArchiveError(f"{m.name} did not unpack correctly from {Path(path).name}")
        out[m.name] = targets[m.name]
    return out
