"""A small S3 client (stdlib only: http.client + hmac / hashlib, AWS Signature Version 4) for the archive on S3.

Written against MEGA S4 (github.com/meganz/s4-specs) and any S3-compatible service. S4 has no ``x-amz-checksum-*`` and no
GetObjectAttributes, so the app's own checksums go into user metadata (``x-amz-meta-*``) and every upload carries ``Content-MD5``
(the service refuses a body that does not match it).

* :class:`Config`: endpoint, region, bucket, prefix and the keys. :func:`load_config` reads ``key=value`` lines (the opt-in test file;
  the app keeps the secret in a file of its own, mode 0600). The endpoint may carry the bucket in front (``<bucket>.s3.example``):
  it is taken off, requests are path-style (``/<bucket>/<key>``), which works whatever the bucket's name.
* :class:`Client`: ``list`` (ListObjectsV2, every page), ``head``, ``put_file`` (one PUT up to ``multipart_threshold``, else a
  multipart upload whose parts each carry their MD5; aborted on any failure), ``get_file`` (to a ``.part`` name, then renamed;
  ``Range`` supported by ``get_bytes``), ``delete`` / ``delete_many``, ``copy`` (metadata kept or replaced), ``list_uploads`` /
  ``abort_upload`` (unfinished multipart uploads). Every request is retried with back-off on a network error or a 5xx / 429 answer.
  Nothing reads a whole file into memory: bodies are streamed from and to disk.
* :func:`probe`: the "Test connection" check: list, put a small object with metadata, head it (is the ETag its MD5? is the
  metadata back?), read it, delete it.

Secrets are never put into an error message, a log line or a result.
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import hmac
import http.client
import os
import socket
import ssl
import time
import urllib.parse
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, Callable, Dict, Iterator, List, Optional, Tuple

__all__ = ["Config", "Client", "S3Error", "load_config", "probe", "sign_v4", "EMPTY_SHA256", "META_PREFIX"]

EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
META_PREFIX = "x-amz-meta-"
CHUNK = 1 << 20
DEFAULT_PART = 64 << 20                 # 64 MiB parts (S3: 5 MiB to 5 GiB, at most 10,000 parts)
RETRIES = 5
RETRY_STATUS = {429, 500, 502, 503, 504}


class S3Error(Exception):
    """An error answer (``status``, the S3 ``code`` and ``message``) or a request that could not be made (``status`` 0)."""

    def __init__(self, status: int, code: str, message: str, region: str = "") -> None:
        super().__init__(f"{code or 'Error'} ({status}): {message}" if status else message)
        self.status, self.code, self.message, self.region = status, code, message, region


@dataclass
class Config:
    endpoint: str                       # host, optionally with a scheme and a port: "s3.g.megas4.com", "http://127.0.0.1:9000"
    bucket: str
    access_key: str = field(repr=False)
    secret_key: str = field(repr=False)
    region: str = ""                    # "" = asked from the service once (see Client.region)
    prefix: str = ""                    # every key of the app's goes below it ("" or "folder/")

    def __post_init__(self) -> None:
        ep = self.endpoint.strip().rstrip("/")
        if "://" not in ep:
            ep = "https://" + ep
        u = urllib.parse.urlsplit(ep)
        host = u.hostname or ""
        if self.bucket and host.lower().startswith(self.bucket.lower() + "."):
            host = host[len(self.bucket) + 1:]                  # "<bucket>.s3.example" -> "s3.example" (requests are path-style)
        self.scheme, self.host, self.port = u.scheme or "https", host, u.port
        self.prefix = self.prefix.strip("/") + "/" if self.prefix.strip("/") else ""

    @property
    def netloc(self) -> str:
        return f"{self.host}:{self.port}" if self.port else self.host


def load_config(path: Path) -> Config:
    """A ``key=value`` file (endpoint, region, bucket, prefix, access_key, secret_key); ``#`` starts a comment."""
    values: Dict[str, str] = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        values[k.strip().lower()] = v.strip()
    missing = [k for k in ("endpoint", "bucket", "access_key", "secret_key") if not values.get(k)]
    if missing:
        raise ValueError(f"{path}: missing {', '.join(missing)}")
    return Config(values["endpoint"], values["bucket"], values["access_key"], values["secret_key"],
                  values.get("region", ""), values.get("prefix", ""))


# --------------------------------------------------------------------------- Signature Version 4
def _quote(s: str, safe: str = "-_.~") -> str:
    return urllib.parse.quote(s, safe=safe)


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def sign_v4(method: str, host: str, path: str, query: Dict[str, str], headers: Dict[str, str], payload_hash: str,
            access_key: str, secret_key: str, region: str, when: datetime.datetime, service: str = "s3") -> Dict[str, str]:
    """The headers to send (``headers`` + host, x-amz-date, x-amz-content-sha256, Authorization). ``path`` is already
    URI-encoded (each segment once, ``/`` kept); header names are compared lower-case."""
    amz_date = when.strftime("%Y%m%dT%H%M%SZ")
    day = amz_date[:8]
    out = {k: v for k, v in headers.items()}
    out["host"] = host
    out["x-amz-date"] = amz_date
    out["x-amz-content-sha256"] = payload_hash
    canon = {k.lower(): " ".join(str(v).strip().split()) for k, v in out.items()}
    signed = ";".join(sorted(canon))
    cq = "&".join(f"{_quote(k)}={_quote(v)}" for k, v in sorted(query.items()))
    creq = "\n".join([method, path or "/", cq, "".join(f"{k}:{canon[k]}\n" for k in sorted(canon)), signed, payload_hash])
    scope = f"{day}/{region}/{service}/aws4_request"
    sts = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(creq.encode("utf-8")).hexdigest()])
    key = _hmac(_hmac(_hmac(_hmac(("AWS4" + secret_key).encode("utf-8"), day), region), service), "aws4_request")
    sig = hmac.new(key, sts.encode("utf-8"), hashlib.sha256).hexdigest()
    out["Authorization"] = f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, SignedHeaders={signed}, Signature={sig}"
    return out


# --------------------------------------------------------------------------- the client
@dataclass
class ObjectInfo:
    key: str                            # the full key (with the prefix)
    size: int
    etag: str                           # without quotes
    last_modified: str = ""
    metadata: Dict[str, str] = field(default_factory=dict)      # x-amz-meta-* without the prefix (head only)

    @property
    def md5(self) -> str:
        """The ETag when it is an MD5 (a single-part upload without encryption): 32 hex digits; else ""."""
        e = self.etag.lower()
        return e if len(e) == 32 and all(c in "0123456789abcdef" for c in e) else ""


def file_digests(path: Path, start: int = 0, length: Optional[int] = None, also: Any = None) -> Tuple[str, str, int]:
    """(md5 hex, sha256 hex, size) of a file or a byte range of it, in one pass (``also``: a hash object fed the same bytes)."""
    md5, sha = hashlib.md5(), hashlib.sha256()
    n = 0
    with open(path, "rb") as f:
        f.seek(start)
        left = length
        while left is None or left > 0:
            block = f.read(CHUNK if left is None else min(CHUNK, left))
            if not block:
                break
            md5.update(block)
            sha.update(block)
            if also is not None:
                also.update(block)
            n += len(block)
            if left is not None:
                left -= len(block)
    return md5.hexdigest(), sha.hexdigest(), n


def _b64md5(hexdigest: str) -> str:
    return base64.b64encode(bytes.fromhex(hexdigest)).decode("ascii")


def _strip_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _xml(data: bytes) -> ET.Element:
    root = ET.fromstring(data)
    for el in root.iter():
        el.tag = _strip_ns(el.tag)
    return root


class Client:
    def __init__(self, config: Config, timeout: float = 60.0, multipart_threshold: int = DEFAULT_PART,
                 part_size: int = DEFAULT_PART, sleep: Callable[[float], None] = time.sleep) -> None:
        self.cfg = config
        self.timeout = timeout
        self.multipart_threshold = max(multipart_threshold, 5 << 20)
        self.part_size = max(part_size, 5 << 20)
        self._sleep = sleep
        self._region = config.region

    # ---- plumbing
    @property
    def region(self) -> str:
        """The signing region: the configured one, else the one the service names in its answer to a request signed for
        ``us-east-1`` (asked once)."""
        if not self._region:
            self._region = "us-east-1"
            try:
                self._request("GET", "", {"list-type": "2", "max-keys": "1"})
            except S3Error as exc:
                if exc.region:
                    self._region = exc.region
                elif exc.status not in (301, 400):
                    raise
        return self._region

    def key(self, rel: str) -> str:
        """The full key of ``rel`` (below the prefix)."""
        return self.cfg.prefix + rel.lstrip("/")

    def _path(self, key: str) -> str:
        return "/" + _quote(self.cfg.bucket, safe="") + ("/" + _quote(key, safe="/-_.~") if key else "")

    def _connect(self) -> http.client.HTTPConnection:
        if self.cfg.scheme == "http":
            return http.client.HTTPConnection(self.cfg.host, self.cfg.port, timeout=self.timeout)
        return http.client.HTTPSConnection(self.cfg.host, self.cfg.port, timeout=self.timeout, context=ssl.create_default_context())

    def _request(self, method: str, key: str, query: Optional[Dict[str, str]] = None, headers: Optional[Dict[str, str]] = None,
                 body: Optional[Callable[[], Any]] = None, payload_hash: str = EMPTY_SHA256, sink: Optional[IO[bytes]] = None,
                 ok: Tuple[int, ...] = (200, 204, 206)) -> Tuple[int, Dict[str, str], bytes]:
        """One request, retried. ``body`` makes a fresh body each try (bytes or an open file). With ``sink`` the answer's body is
        written there (rewound for a retry) and ``b""`` returned."""
        query = dict(query or {})
        path = self._path(key)
        url = path + ("?" + "&".join(f"{_quote(k)}={_quote(v)}" for k, v in sorted(query.items())) if query else "")
        last: Optional[Exception] = None
        for attempt in range(RETRIES):
            if attempt:
                self._sleep(min(30.0, 0.5 * 2 ** (attempt - 1)))
            region = self._region or "us-east-1"
            hdrs = sign_v4(method, self.cfg.netloc, path, query, dict(headers or {}), payload_hash,
                           self.cfg.access_key, self.cfg.secret_key, region, datetime.datetime.now(datetime.timezone.utc))
            conn = self._connect()
            data = body() if body is not None else None
            try:
                conn.request(method, url, body=data, headers=hdrs)
                resp = conn.getresponse()
                status = resp.status
                rhead = {k.lower(): v for k, v in resp.getheaders()}
                if status in ok and sink is not None:
                    sink.seek(0)
                    sink.truncate()
                    while True:
                        block = resp.read(CHUNK)
                        if not block:
                            break
                        sink.write(block)
                    return status, rhead, b""
                payload = resp.read()
            except (OSError, http.client.HTTPException, socket.timeout) as exc:
                last = exc
                continue
            finally:
                if hasattr(data, "close"):
                    data.close()
                conn.close()
            if status in ok:
                return status, rhead, payload
            err = self._error(status, rhead, payload, method)
            if status in RETRY_STATUS:
                last = err
                continue
            raise err
        if isinstance(last, S3Error):
            raise last
        raise S3Error(0, "", f"{self.cfg.host}: {last}")

    @staticmethod
    def _error(status: int, headers: Dict[str, str], payload: bytes, method: str) -> S3Error:
        code, message, region = "", "", headers.get("x-amz-bucket-region", "")
        if payload:
            try:
                root = _xml(payload)
                code = root.findtext("Code") or ""
                message = root.findtext("Message") or ""
                region = root.findtext("Region") or root.findtext("ExpectedRegion") or region
            except ET.ParseError:
                message = payload[:200].decode("utf-8", "replace")
        if not code:
            code = {403: "AccessDenied", 404: "NotFound"}.get(status, "")
        if not message and method == "HEAD":
            message = "no such object" if status == 404 else "refused"
        return S3Error(status, code, message, region)

    # ---- objects
    def list(self, rel_prefix: str = "") -> Iterator[ObjectInfo]:
        """Every object below ``prefix + rel_prefix`` (all pages)."""
        _ = self.region
        token = ""
        while True:
            q = {"list-type": "2", "prefix": self.key(rel_prefix)}
            if token:
                q["continuation-token"] = token
            _s, _h, data = self._request("GET", "", q)
            root = _xml(data)
            for c in root.findall("Contents"):
                yield ObjectInfo(c.findtext("Key") or "", int(c.findtext("Size") or 0), (c.findtext("ETag") or "").strip('"'),
                                 c.findtext("LastModified") or "")
            token = root.findtext("NextContinuationToken") or ""
            if (root.findtext("IsTruncated") or "").lower() != "true" or not token:
                return

    def ping(self) -> None:
        """One cheap signed request (list one key below the prefix): raises S3Error when the bucket cannot be reached or read."""
        _ = self.region
        self._request("GET", "", {"list-type": "2", "max-keys": "1", "prefix": self.cfg.prefix})

    def head(self, key: str) -> Optional[ObjectInfo]:
        """The object's size, ETag and metadata; None when there is none."""
        _ = self.region
        try:
            _s, h, _b = self._request("HEAD", key)
        except S3Error as exc:
            if exc.status == 404:
                return None
            raise
        meta = {k[len(META_PREFIX):]: v for k, v in h.items() if k.startswith(META_PREFIX)}
        return ObjectInfo(key, int(h.get("content-length", "0") or 0), h.get("etag", "").strip('"'), h.get("last-modified", ""), meta)

    def put_bytes(self, key: str, data: bytes, metadata: Optional[Dict[str, str]] = None) -> str:
        """A small object; returns its ETag."""
        _ = self.region
        md5 = hashlib.md5(data).hexdigest()
        headers = {"Content-MD5": _b64md5(md5), "Content-Length": str(len(data)), **self._meta_headers(metadata)}
        _s, h, _b = self._request("PUT", key, headers=headers, body=lambda: data, payload_hash=hashlib.sha256(data).hexdigest())
        return h.get("etag", "").strip('"')

    def put_file(self, key: str, path: Path, metadata: Optional[Dict[str, str]] = None,
                 progress: Optional[Callable[[int, int], None]] = None, digests: Optional[Tuple[str, str]] = None) -> Dict[str, Any]:
        """Upload a file (streamed). Returns ``{"etag", "md5", "size", "parts"}``; ``md5`` is the file's own (hex). ``digests``:
        its (md5, sha256) when the caller read them already (a single-part upload then reads the file once more, to send it)."""
        _ = self.region
        path = Path(path)
        size = path.stat().st_size
        if size <= self.multipart_threshold:
            md5, sha, n = (digests[0], digests[1], size) if digests else file_digests(path)
            headers = {"Content-MD5": _b64md5(md5), "Content-Length": str(n), **self._meta_headers(metadata)}
            _s, h, _b = self._request("PUT", key, headers=headers, body=lambda: open(path, "rb"), payload_hash=sha)
            if progress:
                progress(size, size)
            return {"etag": h.get("etag", "").strip('"'), "md5": md5, "size": size, "parts": 1}
        return self._multipart(key, path, size, metadata, progress)

    def _multipart(self, key: str, path: Path, size: int, metadata: Optional[Dict[str, str]],
                   progress: Optional[Callable[[int, int], None]]) -> Dict[str, Any]:
        part = max(self.part_size, -(-size // 10000))
        _s, _h, data = self._request("POST", key, {"uploads": ""}, headers=self._meta_headers(metadata))
        upload = _xml(data).findtext("UploadId") or ""
        if not upload:
            raise S3Error(0, "", "the service gave no upload id")
        whole = hashlib.md5()
        done: List[Tuple[int, str]] = []
        try:
            offset, number = 0, 1
            while offset < size:
                length = min(part, size - offset)
                md5, sha, n = file_digests(path, offset, length, also=whole)    # (the whole file's MD5 on the way)

                def body(start: int = offset, count: int = length) -> Any:
                    return _Slice(path, start, count)
                headers = {"Content-MD5": _b64md5(md5), "Content-Length": str(n)}
                _s, h, _b = self._request("PUT", key, {"partNumber": str(number), "uploadId": upload}, headers=headers, body=body,
                                          payload_hash=sha)
                done.append((number, h.get("etag", "").strip('"')))
                offset += length
                number += 1
                if progress:
                    progress(offset, size)
            xml = ("<CompleteMultipartUpload>" + "".join(f"<Part><PartNumber>{n}</PartNumber><ETag>\"{e}\"</ETag></Part>"
                                                         for n, e in done) + "</CompleteMultipartUpload>").encode("utf-8")
            _s, _h, data = self._request("POST", key, {"uploadId": upload}, headers={"Content-Length": str(len(xml))},
                                         body=lambda: xml, payload_hash=hashlib.sha256(xml).hexdigest())
            root = _xml(data)
            if root.tag == "Error":                              # (S3 can answer 200 with an error in the body)
                raise S3Error(200, root.findtext("Code") or "", root.findtext("Message") or "")
            etag = (root.findtext("ETag") or "").strip('"')
        except BaseException:
            try:
                self.abort_upload(key, upload)
            except S3Error:
                pass
            raise
        return {"etag": etag, "md5": whole.hexdigest(), "size": size, "parts": len(done)}

    def get_bytes(self, key: str, start: Optional[int] = None, end: Optional[int] = None) -> bytes:
        """The object, or the bytes ``start`` to ``end`` (inclusive) of it."""
        _ = self.region
        headers = {}
        if start is not None:
            headers["Range"] = f"bytes={start}-{'' if end is None else end}"
        _s, _h, data = self._request("GET", key, headers=headers)
        return data

    def get_file(self, key: str, dest: Path) -> Dict[str, Any]:
        """Download to ``dest`` (written as ``<dest>.romorg.part``, renamed when complete; never over an existing file).
        Returns ``{"size", "md5", "etag"}`` (the md5 of what arrived)."""
        _ = self.region
        dest = Path(dest)
        if os.path.lexists(dest):
            raise FileExistsError(f"a file is already there: {dest}")
        part = dest.with_name(dest.name + ".romorg.part")
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(part, "w+b") as f:
                _s, h, _b = self._request("GET", key, sink=f)
            md5, _sha, size = file_digests(part)
            os.link(part, dest) if hasattr(os, "link") else os.rename(part, dest)
        finally:
            if part.exists():
                part.unlink()
        return {"size": size, "md5": md5, "etag": h.get("etag", "").strip('"')}

    def delete(self, key: str) -> None:
        _ = self.region
        self._request("DELETE", key)

    def delete_many(self, keys: List[str]) -> List[Dict[str, str]]:
        """Delete up to any number of keys (1,000 a request); returns the errors (``{key, code, message}``)."""
        _ = self.region
        errors: List[Dict[str, str]] = []
        for i in range(0, len(keys), 1000):
            batch = keys[i:i + 1000]
            xml = ("<Delete><Quiet>true</Quiet>" + "".join(f"<Object><Key>{_xml_escape(k)}</Key></Object>" for k in batch)
                   + "</Delete>").encode("utf-8")
            md5 = hashlib.md5(xml).hexdigest()
            _s, _h, data = self._request("POST", "", {"delete": ""}, headers={"Content-MD5": _b64md5(md5), "Content-Length": str(len(xml))},
                                         body=lambda x=xml: x, payload_hash=hashlib.sha256(xml).hexdigest())
            if data:
                for e in _xml(data).findall("Error"):
                    errors.append({"key": e.findtext("Key") or "", "code": e.findtext("Code") or "", "message": e.findtext("Message") or ""})
        return errors

    def copy(self, src_key: str, dst_key: str, metadata: Optional[Dict[str, str]] = None) -> str:
        """Server-side copy (metadata kept, or replaced by ``metadata``); returns the new ETag."""
        _ = self.region
        headers = {"x-amz-copy-source": "/" + _quote(self.cfg.bucket, safe="") + "/" + _quote(src_key, safe="/-_.~")}
        if metadata is not None:
            headers["x-amz-metadata-directive"] = "REPLACE"
            headers.update(self._meta_headers(metadata))
        _s, _h, data = self._request("PUT", dst_key, headers=headers)
        root = _xml(data)
        if root.tag == "Error":
            raise S3Error(200, root.findtext("Code") or "", root.findtext("Message") or "")
        return (root.findtext("ETag") or "").strip('"')

    def list_uploads(self) -> List[Dict[str, str]]:
        """Unfinished multipart uploads below the prefix: ``{key, upload_id, initiated}``."""
        _ = self.region
        _s, _h, data = self._request("GET", "", {"uploads": "", "prefix": self.cfg.prefix})
        return [{"key": u.findtext("Key") or "", "upload_id": u.findtext("UploadId") or "", "initiated": u.findtext("Initiated") or ""}
                for u in _xml(data).findall("Upload")]

    def abort_upload(self, key: str, upload_id: str) -> None:
        self._request("DELETE", key, {"uploadId": upload_id})

    @staticmethod
    def _meta_headers(metadata: Optional[Dict[str, str]]) -> Dict[str, str]:
        """``x-amz-meta-<name>`` headers; a value that is not plain ASCII is sent percent-encoded (and marked so)."""
        out: Dict[str, str] = {}
        for k, v in (metadata or {}).items():
            v = str(v)
            if not v.isascii() or any(ord(c) < 32 for c in v):
                v = "url:" + _quote(v)
            out[META_PREFIX + k.lower()] = v
        return out


class _Slice:
    """A read-only window ``[start, start + count)`` of a file, for http.client to stream a part from."""

    def __init__(self, path: Path, start: int, count: int) -> None:
        self._f = open(path, "rb")
        self._f.seek(start)
        self._left = count

    def read(self, n: int = -1) -> bytes:
        if self._left <= 0:
            return b""
        n = self._left if n is None or n < 0 else min(n, self._left)
        data = self._f.read(n)
        self._left -= len(data)
        return data

    def close(self) -> None:
        self._f.close()


def _xml_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;").replace("'", "&apos;")


def meta_value(v: str) -> str:
    """A metadata value as it was given (undoes the ``url:`` encoding of :meth:`Client._meta_headers`)."""
    return urllib.parse.unquote(v[4:]) if v.startswith("url:") else v


# --------------------------------------------------------------------------- "Test connection"
def probe(client: Client) -> Dict[str, Any]:
    """List, put a small object with metadata, head it, read it back, delete it. Returns what was found; raises S3Error when
    the service refuses a step (``step`` says which in the result of a caught error)."""
    out: Dict[str, Any] = {"region": "", "listed": None, "etag_is_md5": None, "metadata_back": None, "read_back": None,
                           "range_read": None, "deleted": None}
    out["region"] = client.region
    out["listed"] = sum(1 for _ in zip(range(1000), client.list()))
    key = client.key(f".romorg-probe-{uuid.uuid4().hex[:12]}")
    body = b"simple-rom-organiser connection test\n" * 8
    md5 = hashlib.md5(body).hexdigest()
    meta = {"sha1": hashlib.sha1(body).hexdigest(), "romorg": "probe", "name": "Pokémon (Europe).zip"}
    try:
        etag = client.put_bytes(key, body, meta)
        info = client.head(key)
        out["etag_is_md5"] = bool(info) and info.etag.lower() == md5 and etag.lower() == md5
        got = {k: meta_value(v) for k, v in (info.metadata if info else {}).items()}
        out["metadata_back"] = all(got.get(k) == v for k, v in meta.items())
        out["read_back"] = client.get_bytes(key) == body
        out["range_read"] = client.get_bytes(key, 8, 17) == body[8:18]
    finally:
        client.delete(key)
    out["deleted"] = client.head(key) is None
    return out
