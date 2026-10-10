"""A finished scan, kept on disk, so a system you scanned before is there when the app starts again.

One file per system in ``paths.scans_dir()``: the scan result as it was, with what it was made from - the folder (how many files, how
many bytes, the newest change), the databases it was matched against, the options that change a result - and the program that made
it. It is used again only when all of that is unchanged; otherwise it is as if there were none (and the scan, which reads only what
changed, is as quick as ever). A file that cannot be read - another version, damaged - is deleted. Nothing here ever raises.

The files are the app's own (a pickle in its data folder, like any cache of Python objects): the app does not read one from
anywhere else.
"""

from __future__ import annotations

import dataclasses
import hashlib
import os
import pickle
import re
import tempfile
import traceback
from pathlib import Path
from typing import Any, Dict, Optional

from . import __version__, paths

__all__ = ["folder_stamp", "save", "load", "forget"]

FORMAT = 1


def folder_stamp(root: Path) -> tuple:
    """``(files, bytes, newest change)`` of everything under ``root`` (folders count by their change time too): the same numbers
    mean nothing was added, removed, renamed or rewritten."""
    files = size = newest = 0
    stack = [os.fspath(root)]
    while stack:
        try:
            with os.scandir(stack.pop()) as it:
                for de in it:
                    try:
                        st = de.stat(follow_symlinks=False)
                        newest = max(newest, st.st_mtime_ns)
                        if de.is_dir(follow_symlinks=False):
                            stack.append(de.path)
                        else:
                            files += 1
                            size += st.st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return (files, size, newest)


def _shape() -> str:
    """The fields of the classes a scan result is made of: a change to any of them makes an older file useless."""
    from . import datfile, discsys, scanner
    parts = []
    for module in (scanner, discsys, datfile):
        for name in sorted(vars(module)):
            obj = getattr(module, name)
            if isinstance(obj, type) and dataclasses.is_dataclass(obj) and obj.__module__ == module.__name__:
                parts.append(f"{module.__name__}.{name}:" + ",".join(f.name for f in dataclasses.fields(obj)))
    return hashlib.sha1("\n".join(parts).encode()).hexdigest()


def _path(name: str) -> Path:
    return paths.scans_dir() / (re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_") + ".scan")


def forget(name: str) -> None:
    try:
        _path(name).unlink()
    except OSError:
        pass


def save(name: str, root: Path, stamp: tuple, dats_sig: tuple, options: tuple, result: Any, extra: Dict[str, Any]) -> bool:
    """Keep the scan of the system ``name``. False when it could not be (it is then only not kept)."""
    target = _path(name)
    tmp = ""
    try:
        payload = {"format": FORMAT, "version": __version__, "shape": _shape(), "name": name, "root": os.fspath(root),
                   "stamp": tuple(stamp), "dats_sig": tuple(dats_sig), "options": tuple(options), "extra": extra, "result": result}
        fd, tmp = tempfile.mkstemp(prefix=".scan-", suffix=".tmp", dir=str(target.parent))
        with os.fdopen(fd, "wb") as fh:
            pickle.dump(payload, fh, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(tmp, target)
        return True
    except Exception:  # noqa: BLE001 - a convenience: the scan is simply not kept
        traceback.print_exc()
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        return False


def load(name: str, root: Path, dats_sig: tuple, options: tuple) -> Optional[Dict[str, Any]]:
    """The kept scan of ``name`` (``{"result", "stamp", "extra"}``) when nothing it was made from has changed, else None."""
    target = _path(name)
    if not target.is_file():
        return None
    try:
        with target.open("rb") as fh:
            payload = pickle.load(fh)
        if (payload["format"], payload["version"], payload["shape"]) != (FORMAT, __version__, _shape()):
            raise ValueError("made by another version")
        if os.path.normcase(payload["root"]) != os.path.normcase(os.fspath(root)):
            return None                                        # (another folder: this file stays for when it is chosen back)
        if payload["dats_sig"] != tuple(dats_sig) or payload["options"] != tuple(options):
            return None
        stamp = folder_stamp(root)
        if payload["stamp"] != stamp:
            return None
        payload["result"].summary()                            # (a result that cannot even be summed up is no result)
        return {"result": payload["result"], "stamp": stamp, "extra": payload["extra"]}
    except Exception:  # noqa: BLE001 - damaged, or made by other code: gone
        forget(name)
        return None
