"""The archive in an S3 bucket: what a build sets aside is uploaded, checked and then deleted from the disk (the user: "bin it").

An archive location is a folder or ``s3://<bucket>/<prefix>``. Planning treats an S3 archive as a folder that is never written
(:func:`stand_in`: ``<data>/s3-archive/<bucket>/<prefix>``), so the archive pass, the sweep of old reason folders and the saves of
archived games plan their moves as for a folder; :func:`upload_moves` then turns each move into an upload to the key that is the
move's target below the stand-in. Names are kept free against the bucket (:class:`Inventory`, registered with
``sortroot.register_virtual``), as on a disk.

An upload is done when the object is checked: its size, its ETag (the MD5 of a single-part upload) or the ``md5`` of its metadata,
and its ``sha1``. Only then is the local file deleted. An object that is already there with the same content counts as done (the
local file goes); one with other content is never replaced (the move fails; a plan never targets it, see :class:`Inventory`).
Metadata of every object: ``md5``, ``sha1``, ``crc32``, ``size``, ``from`` (the file's path below the ROM folder, when it came
from there), ``rom`` (the DAT rom or disc game the build identified it as) and, for a zip / 7z, ``roms`` (``crc:size`` of its members, ``;``-separated, as far as 1,500 characters go): enough to
know which game an object holds without downloading it. The metadata of the objects is kept in ``<data>/s3-meta/<bucket>.json``
(a listing has none; an object this app did not upload is read once with a HEAD).

Restoring (:func:`restore`) downloads an object next to its destination (``.romorg.part``), checks its MD5 against the object's
and renames it into place, never over a file. The object stays in the archive.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import zlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from . import s3, sortroot

__all__ = ["Target", "parse", "stand_in", "Inventory", "upload_moves", "display", "STAND_IN_DIR", "WORKERS"]

STAND_IN_DIR = "s3-archive"
WORKERS = 4                              # uploads at once (small files are bound by the round trips, big ones by the line)


@dataclass(frozen=True)
class Target:
    bucket: str
    prefix: str                          # "" or "folder/" (always ends in "/")

    @property
    def url(self) -> str:
        return f"s3://{self.bucket}/{self.prefix}"


def parse(raw: str) -> Optional[Target]:
    """``s3://bucket/prefix`` -> Target; None for anything else (a folder)."""
    raw = (raw or "").strip()
    if not raw.lower().startswith("s3://"):
        return None
    rest = raw[5:].strip("/")
    bucket, _, prefix = rest.partition("/")
    if not bucket or any(c in bucket for c in " \\"):
        raise ValueError("An S3 archive is written s3://<bucket>/<folder> (the folder is optional).")
    prefix = "/".join(p for p in prefix.split("/") if p)
    return Target(bucket, prefix + "/" if prefix else "")


def stand_in(data_dir: Path, target: Target) -> Path:
    """The folder planning uses for this archive (never written: the moves to it are uploads)."""
    return Path(data_dir) / STAND_IN_DIR / target.bucket / Path(*target.prefix.strip("/").split("/")) if target.prefix else \
        Path(data_dir) / STAND_IN_DIR / target.bucket


def display(base: Path, data_dir: Path) -> Optional[str]:
    """``s3://bucket/prefix/...`` for a path below the stand-in folders; None for any other path."""
    try:
        rel = Path(base).relative_to(Path(data_dir) / STAND_IN_DIR)
    except ValueError:
        return None
    return "s3://" + "/".join(rel.parts) + "/"


class Inventory:
    """The objects below an archive's prefix, read per system folder when first asked (one listing each) and kept up to date with
    the uploads of this app. ``exists(path)`` answers for paths below the stand-in folder (registered with sortroot so that the free
    names of a plan avoid the bucket's objects)."""

    def __init__(self, client: Optional[s3.Client], target: Target, base: Path, meta_file: Optional[Path] = None) -> None:
        self.client, self.target, self.base = client, target, Path(base)
        self._tops: Dict[str, Dict[str, s3.ObjectInfo]] = {}
        self._dirs: Dict[str, set] = {}                    # top -> every "folder/" an object lies in (names are checked often)
        self._lock = threading.Lock()
        self.error = ""
        self.version = 0                 # changes whenever what is known changes (the tree is made again)
        self.meta_file = meta_file
        self._meta: Dict[str, Dict[str, Any]] = {}          # key -> {"etag", "size", "meta"} (kept on disk)
        self._meta_dirty = False
        if meta_file is not None:
            try:
                data = json.loads(Path(meta_file).read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self._meta = {k: v for k, v in data.items() if isinstance(v, dict)}
            except (OSError, ValueError):
                pass

    def key(self, path: Path) -> Optional[str]:
        try:
            rel = Path(path).relative_to(self.base)
        except ValueError:
            return None
        return self.target.prefix + "/".join(rel.parts)

    def objects(self, top: str) -> Dict[str, s3.ObjectInfo]:
        """{key: info} below ``<prefix><top>/`` (listed once; an error leaves it empty and sets ``error``)."""
        if self.client is None:                         # (no connection: nothing is known to be there; ``error`` says why)
            return {}
        with self._lock:
            if top not in self._tops:
                try:
                    self._tops[top] = {o.key: o for o in self.client.list(f"{top}/")}
                    self._dirs[top] = set()
                    for k in self._tops[top]:
                        self._note_dirs(top, k)
                    self.version += 1
                except s3.S3Error as exc:
                    self.error = str(exc)
                    return {}
            return self._tops[top]

    def get(self, key: str) -> Optional[s3.ObjectInfo]:
        top = key[len(self.target.prefix):].split("/", 1)[0]
        return self.objects(top).get(key)

    def exists(self, path: Path) -> bool:
        k = self.key(path)
        if k is None:
            return os.path.lexists(path)
        top = k[len(self.target.prefix):].split("/", 1)[0]
        objs = self.objects(top)
        return k in objs or (k.rstrip("/") + "/") in self._dirs.get(top, ())

    def _note_dirs(self, top: str, key: str) -> None:
        dirs = self._dirs.setdefault(top, set())
        cut = key.rfind("/")
        while cut > 0:
            d = key[:cut + 1]
            if d in dirs:
                break
            dirs.add(d)
            cut = key.rfind("/", 0, cut)

    def add(self, info: s3.ObjectInfo) -> None:
        top = info.key[len(self.target.prefix):].split("/", 1)[0]
        with self._lock:
            if top in self._tops:
                self._tops[top][info.key] = info
                self._note_dirs(top, info.key)
                self.version += 1
            if info.metadata:
                self._meta[info.key] = {"etag": info.etag, "size": info.size, "meta": dict(info.metadata)}
                self._meta_dirty = True

    def has_meta(self, key: str) -> bool:
        """The object's metadata is known (kept from an upload or an earlier HEAD, for the ETag it has now)."""
        listed = self.get(key)
        kept = self._meta.get(key)
        return kept is not None and (listed is None or kept.get("etag") == listed.etag)

    def metadata(self, key: str, fetch: bool = True) -> Dict[str, str]:
        """The object's metadata: from the kept copy while its ETag is the listing's, else (``fetch``) read with a HEAD."""
        listed = self.get(key)
        kept = self._meta.get(key)
        if kept is not None and (listed is None or kept.get("etag") == listed.etag):
            return dict(kept.get("meta") or {})
        if not fetch or self.client is None or listed is None:
            return {}
        info = self.client.head(key)
        if info is None:
            return {}
        with self._lock:
            self._meta[key] = {"etag": info.etag, "size": info.size, "meta": dict(info.metadata)}
            self._meta_dirty = True
        return dict(info.metadata)

    def save_meta(self) -> None:
        """Write the kept metadata (only the keys still listed, for the folders read) when it changed."""
        if self.meta_file is None or not self._meta_dirty:
            return
        with self._lock:
            data = dict(self._meta)
            self._meta_dirty = False
        path = Path(self.meta_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, path)

    def files(self, top: str) -> List[Tuple[Tuple[str, ...], int]]:
        """``(parts below the stand-in's top folder, size)`` of every object of one system folder (the tree's "already archived")."""
        out = []
        for k, o in sorted(self.objects(top).items()):
            rel = k[len(self.target.prefix):].split("/")[1:]
            if rel and rel[-1]:
                out.append((tuple(rel), o.size))
        return out


def _digests(path: Path) -> Dict[str, Any]:
    md5, sha1, sha256, crc, n = hashlib.md5(), hashlib.sha1(), hashlib.sha256(), 0, 0
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(s3.CHUNK), b""):
            md5.update(block)
            sha1.update(block)
            sha256.update(block)
            crc = zlib.crc32(block, crc)
            n += len(block)
    return {"md5": md5.hexdigest(), "sha1": sha1.hexdigest(), "sha256": sha256.hexdigest(), "crc32": f"{crc & 0xffffffff:08x}", "size": n}


def _same(info: s3.ObjectInfo, d: Dict[str, Any]) -> bool:
    if info.size != d["size"]:
        return False
    if info.md5:
        return info.md5 == d["md5"]
    meta = info.metadata
    return bool(meta) and meta.get("md5") == d["md5"]


def _upload_one(client: s3.Client, inv: Inventory, m: Any, key: str, root: Optional[Path]) -> Tuple[str, int]:
    """Upload, check, and (a move) delete the local file. Returns ("moved" | "already", bytes sent). A copy whose source is in the
    archive itself (a save of several games: a copy under each of the others) is made by the service."""
    src = Path(m.src)
    src_key = inv.key(src)
    if src_key is not None:
        if inv.get(key) is not None:
            raise FileExistsError(f"another object is already at {key}")
        client.copy(src_key, key)
        full = client.head(key)
        orig = client.head(src_key)
        if full is None or orig is None or full.size != orig.size:
            raise OSError(f"the copy at {key} does not check out")
        inv.add(full)
        return "moved", 0
    d = _digests(src)
    have = inv.get(key)
    if have is not None:
        full = client.head(key) or have
        if not _same(full, d):
            raise FileExistsError(f"another object is already at {key}")
        sent, how = 0, "already"
    else:
        meta = {"md5": d["md5"], "sha1": d["sha1"], "crc32": d["crc32"], "size": str(d["size"])}
        roms = member_crcs(src)
        if roms:
            meta["roms"] = roms
        if getattr(m, "rom", ""):                    # (what the build identified it as: a CHD or a game folder is found by this name)
            meta["rom"] = m.rom
        if root is not None:
            try:
                meta["from"] = src.relative_to(root).as_posix()
            except ValueError:
                pass
        r = client.put_file(key, src, meta, digests=(d["md5"], d["sha256"]))
        if r["md5"] != d["md5"]:
            raise OSError("the file changed while it was uploaded")
        full = client.head(key)
        if full is None or not _same(full, d) or full.metadata.get("sha1") != d["sha1"]:
            raise OSError(f"the object at {key} does not check out after the upload")
        sent, how = d["size"], "moved"
    inv.add(full)
    if not getattr(m, "copy", False):
        sortroot.remove_file(src)
    return how, sent


def upload_moves(client: s3.Client, inv: Inventory, moves: List[Any], keep: Iterable[Path] = (), root: Optional[Path] = None,
                 progress: Optional[Callable[[int, int, str], None]] = None, cancel: Optional[Callable[[], bool]] = None,
                 workers: int = WORKERS) -> Dict[str, Any]:
    """Do ``moves`` (``sortroot.SMove``, targets below ``inv.base``) as uploads; folders emptied in the ROM folder are removed
    (``keep`` never). Returns ``{"moved", "failed": [{path, error}], "cancelled", "uploaded", "bytes", "already"}``."""
    res: Dict[str, Any] = {"moved": 0, "failed": [], "cancelled": False, "uploaded": 0, "bytes": 0, "already": 0}
    jobs: List[Tuple[Any, str]] = []
    for m in moves:
        src = Path(m.src)
        files = [src] if inv.key(src) is not None or not src.is_dir() else sorted(p for p in src.rglob("*") if p.is_file())
        for f in files:
            dst = Path(m.dst) / f.relative_to(src) if f != src else Path(m.dst)
            key = inv.key(dst)
            if key is None:
                res["failed"].append({"path": str(f), "error": "not a target in the S3 archive"})
                continue
            jobs.append((sortroot.SMove(f, dst, m.bucket, m.note, "file", getattr(m, "size", 0), getattr(m, "copy", False),
                                        getattr(m, "rom", "")), key))
    emptied: set = set()
    total = len(jobs)
    done = 0
    copies = [j for j in jobs if j[0].copy]            # (a copy is made of a file this run uploads: after it)
    singles = [j for j in jobs if not j[0].copy]
    for batch in (singles, copies):
        with ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="romorg-s3") as pool:
            futures = {}
            for m, key in batch:
                if cancel and cancel():
                    res["cancelled"] = True
                    break
                futures[pool.submit(_upload_one, client, inv, m, key, root)] = m
            for fut in as_completed(futures):
                m = futures[fut]
                done += 1
                if progress:
                    progress(done, total, Path(m.src).name)
                try:
                    how, sent = fut.result()
                    res["moved"] += 1
                    res["already" if how == "already" else "uploaded"] += 1
                    res["bytes"] += sent
                    if not m.copy:
                        emptied.add(Path(m.src).parent)
                except (OSError, ValueError, s3.S3Error) as exc:
                    res["failed"].append({"path": str(m.src), "error": str(exc)})
        if res["cancelled"]:
            break
    sortroot._remove_empty_dirs(emptied, keep)
    return res


def member_crcs(path: Path) -> str:
    """``crc:size;crc:size...`` of a zip's / 7z's members (as far as 1,500 characters go); "" for any other file or one that cannot
    be read."""
    if Path(path).suffix.lower() not in (".zip", ".7z"):
        return ""
    try:
        from . import discarchive
        members = discarchive.list_members(Path(path))
    except Exception:  # noqa: BLE001 - a courtesy: the object is matched by its own checksums then
        return ""
    out = ""
    for m in members:
        part = f"{m.crc}:{m.size}"
        if len(out) + len(part) + 1 > 1500:
            break
        out = f"{out};{part}" if out else part
    return out


def restore(client: s3.Client, inv: Inventory, key: str, dest: Path) -> Dict[str, Any]:
    """Download ``key`` to ``dest`` (never over a file), checked against the object's MD5 (its ETag, or the ``md5`` of its metadata).
    Returns ``{"size", "md5"}``; the object stays in the archive."""
    meta = inv.metadata(key)
    listed = inv.get(key)
    want = (listed.md5 if listed is not None else "") or meta.get("md5", "")
    got = client.get_file(key, dest)
    if want and got["md5"] != want:
        try:
            os.unlink(dest)
        except OSError:
            pass
        raise OSError(f"{key}: what arrived does not match the object (MD5)")
    return {"size": got["size"], "md5": got["md5"]}


def abort_stale_uploads(client: s3.Client) -> int:
    """Abort the unfinished multipart uploads below the prefix (a run that was killed); only one build runs at a time."""
    n = 0
    for u in client.list_uploads():
        try:
            client.abort_upload(u["key"], u["upload_id"])
            n += 1
        except s3.S3Error:
            pass
    return n

