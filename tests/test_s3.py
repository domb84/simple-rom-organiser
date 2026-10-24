"""Tests for romorg.s3: SigV4 (AWS's own example), and every call against an S3 server in the tests (http.server, the subset the
app uses, each request's signature checked). ``ROMORG_S4_TEST=<config file>`` also runs the probe against a real service."""

from __future__ import annotations

import datetime
import hashlib
import base64
import os
import re
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from romorg import s3

AK, SK, REGION, BUCKET = "AKTEST", "secret/TEST+key", "eu-test", "bucket"


class FakeS3:
    """Objects in memory: {key: (bytes, metadata)}; multipart uploads; ``fail`` = statuses to answer before the next good one."""

    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, dict[str, str]]] = {}
        self.uploads: dict[str, tuple[str, dict[int, bytes], dict[str, str]]] = {}
        self.fail: list[int] = []
        self.requests: list[str] = []
        self.page = 1000
        self.lock = threading.Lock()


def _handler(fake: FakeS3):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a) -> None:  # noqa: D401
            pass

        def _send(self, status: int, body: bytes = b"", headers: dict | None = None) -> None:
            self.send_response(status)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _err(self, status: int, code: str, extra: str = "") -> None:
            self._send(status, f"<Error><Code>{code}</Code><Message>{code}</Message>{extra}</Error>".encode())

        def _body(self) -> bytes:
            n = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(n) if n else b""

        def _check_sig(self, body: bytes) -> bool:
            auth = self.headers.get("Authorization", "")
            m = re.match(r"AWS4-HMAC-SHA256 Credential=([^/]+)/(\d{8})/([^/]+)/s3/aws4_request, SignedHeaders=([^,]+), Signature=(\w+)", auth)
            if not m or m.group(1) != AK:
                self._err(403, "InvalidAccessKeyId")
                return False
            if m.group(3) != REGION:
                self._err(400, "AuthorizationHeaderMalformed", f"<Region>{REGION}</Region>")
                return False
            sha = self.headers.get("x-amz-content-sha256", "")
            if sha != hashlib.sha256(body).hexdigest():
                self._err(400, "XAmzContentSHA256Mismatch")
                return False
            u = urllib.parse.urlsplit(self.path)
            query = dict(urllib.parse.parse_qsl(u.query, keep_blank_values=True))
            names = m.group(4).split(";")
            hdrs = {n: self.headers.get(n, "") for n in names if n not in ("host", "x-amz-date", "x-amz-content-sha256")}
            when = datetime.datetime.strptime(self.headers["x-amz-date"], "%Y%m%dT%H%M%SZ").replace(tzinfo=datetime.timezone.utc)
            want = s3.sign_v4(self.command, self.headers["Host"], u.path, query, hdrs, sha, AK, SK, REGION, when)["Authorization"]
            if want != auth:
                self._err(403, "SignatureDoesNotMatch")
                return False
            return True

        def _do(self) -> None:
            body = self._body()
            with fake.lock:
                fake.requests.append(f"{self.command} {self.path}")
                if fake.fail:
                    self._err(fake.fail.pop(0), "SlowDown")
                    return
            if not self._check_sig(body):
                return
            if "Content-MD5" in self.headers and base64.b64encode(hashlib.md5(body).digest()).decode() != self.headers["Content-MD5"]:
                self._err(400, "BadDigest")
                return
            u = urllib.parse.urlsplit(self.path)
            q = dict(urllib.parse.parse_qsl(u.query, keep_blank_values=True))
            parts = u.path.split("/", 2)
            if parts[1] != BUCKET:
                self._err(404, "NoSuchBucket")
                return
            key = urllib.parse.unquote(parts[2]) if len(parts) > 2 else ""
            meta = {k.lower(): v for k, v in self.headers.items() if k.lower().startswith("x-amz-meta-")}
            c = self.command
            with fake.lock:
                if c == "GET" and not key and "uploads" in q:
                    xml = "".join(f"<Upload><Key>{k}</Key><UploadId>{i}</UploadId><Initiated>x</Initiated></Upload>"
                                  for i, (k, _p, _m) in fake.uploads.items())
                    self._send(200, f"<ListMultipartUploadsResult>{xml}</ListMultipartUploadsResult>".encode())
                elif c == "GET" and not key:
                    keys = sorted(k for k in fake.objects if k.startswith(q.get("prefix", "")))
                    start = int(q.get("continuation-token") or 0)
                    page = keys[start:start + min(fake.page, int(q.get("max-keys", 1000)))]
                    more = start + len(page) < len(keys)
                    xml = "".join(f"<Contents><Key>{s3._xml_escape(k)}</Key><Size>{len(fake.objects[k][0])}</Size>"
                                  f"<ETag>&quot;{hashlib.md5(fake.objects[k][0]).hexdigest()}&quot;</ETag></Contents>" for k in page)
                    tail = f"<IsTruncated>true</IsTruncated><NextContinuationToken>{start + len(page)}</NextContinuationToken>" if more \
                        else "<IsTruncated>false</IsTruncated>"
                    self._send(200, f'<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">{xml}{tail}</ListBucketResult>'.encode())
                elif c == "POST" and not key and "delete" in q:
                    for k in re.findall(r"<Key>(.*?)</Key>", body.decode()):
                        fake.objects.pop(k.replace("&amp;", "&"), None)
                    self._send(200, b"<DeleteResult></DeleteResult>")
                elif c == "POST" and "uploads" in q:
                    uid = f"u{len(fake.uploads) + 1}"
                    fake.uploads[uid] = (key, {}, meta)
                    self._send(200, f"<InitiateMultipartUploadResult><UploadId>{uid}</UploadId></InitiateMultipartUploadResult>".encode())
                elif c == "PUT" and "uploadId" in q:
                    fake.uploads[q["uploadId"]][1][int(q["partNumber"])] = body
                    self._send(200, headers={"ETag": f'"{hashlib.md5(body).hexdigest()}"'})
                elif c == "POST" and "uploadId" in q:
                    k, ps, m = fake.uploads.pop(q["uploadId"])
                    data = b"".join(ps[n] for n in sorted(ps))
                    fake.objects[k] = (data, m)
                    self._send(200, f"<CompleteMultipartUploadResult><ETag>&quot;x-{len(ps)}&quot;</ETag></CompleteMultipartUploadResult>".encode())
                elif c == "DELETE" and "uploadId" in q:
                    fake.uploads.pop(q["uploadId"], None)
                    self._send(204)
                elif c == "PUT" and "x-amz-copy-source" in self.headers:
                    src = urllib.parse.unquote(self.headers["x-amz-copy-source"]).split("/", 2)[2]
                    data, m = fake.objects[src]
                    fake.objects[key] = (data, meta if self.headers.get("x-amz-metadata-directive") == "REPLACE" else dict(m))
                    self._send(200, f"<CopyObjectResult><ETag>&quot;{hashlib.md5(data).hexdigest()}&quot;</ETag></CopyObjectResult>".encode())
                elif c == "PUT":
                    fake.objects[key] = (body, meta)
                    self._send(200, headers={"ETag": f'"{hashlib.md5(body).hexdigest()}"'})
                elif key not in fake.objects:
                    self._err(404, "NoSuchKey")
                elif c == "DELETE":
                    del fake.objects[key]
                    self._send(204)
                else:
                    data, m = fake.objects[key]
                    h = {"ETag": f'"{hashlib.md5(data).hexdigest()}"', **m}
                    rng = self.headers.get("Range")
                    if rng and c == "GET":
                        a, b = rng[6:].split("-")
                        data = data[int(a):(int(b) + 1 if b else None)]
                        self._send(206, data, h)
                    elif c == "HEAD":
                        self.send_response(200)
                        for k, v in h.items():
                            self.send_header(k, v)
                        self.send_header("Content-Length", str(len(data)))
                        self.end_headers()
                    else:
                        self._send(200, data, h)

        do_GET = do_PUT = do_POST = do_DELETE = do_HEAD = _do

    return H


class SigV4(unittest.TestCase):
    def test_the_aws_example_signature(self) -> None:
        h = s3.sign_v4("GET", "examplebucket.s3.amazonaws.com", "/test.txt", {}, {"Range": "bytes=0-9"}, s3.EMPTY_SHA256,
                       "AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "us-east-1",
                       datetime.datetime(2013, 5, 24, tzinfo=datetime.timezone.utc))
        self.assertTrue(h["Authorization"].endswith("Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"))

    def test_the_bucket_in_front_of_the_endpoint_is_taken_off(self) -> None:
        c = s3.Config("emu-test.s3.g.megas4.com", "emu-test", "a", "b")
        self.assertEqual((c.scheme, c.host, c.port), ("https", "s3.g.megas4.com", None))
        self.assertNotIn("b", repr(c).replace("bucket", ""))                       # (no key in a repr)
        self.assertEqual(s3.Config("http://127.0.0.1:9000", "x", "a", "b", prefix="/arch/").prefix, "arch/")

    def test_a_config_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "c.conf"
            p.write_text("# test\nendpoint=s3.example\nbucket=b\naccess_key=a\nsecret_key=s=x\nregion=\n")
            c = s3.load_config(p)
            self.assertEqual((c.host, c.bucket, c.secret_key, c.region), ("s3.example", "b", "s=x", ""))
            p.write_text("endpoint=s3.example\n")
            with self.assertRaises(ValueError):
                s3.load_config(p)


class Calls(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeS3()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), _handler(self.fake))
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.tmp = Path(tempfile.mkdtemp(prefix="romorg-s3-"))
        self.addCleanup(lambda: [p.unlink() for p in self.tmp.iterdir()] and None or self.tmp.rmdir())
        self.client = self.make()

    def make(self, **kw) -> s3.Client:
        cfg = s3.Config(f"http://127.0.0.1:{self.srv.server_address[1]}", BUCKET, AK, SK, prefix="arch")
        return s3.Client(cfg, sleep=lambda s: None, **kw)

    def test_the_region_is_asked_from_the_service_once(self) -> None:
        self.assertEqual(self.client.region, REGION)
        self.assertEqual(self.client.region, REGION)
        self.assertEqual(len(self.fake.requests), 1)

    def test_put_head_get_range_delete_with_metadata(self) -> None:
        k = self.client.key("snes/_excluded/Pokémon & Co (Europe).7z")
        etag = self.client.put_bytes(k, b"0123456789abcdef", {"sha1": "abc", "name": "Pokémon"})
        self.assertEqual(etag, hashlib.md5(b"0123456789abcdef").hexdigest())
        info = self.client.head(k)
        self.assertEqual((info.size, info.md5, info.metadata["sha1"]), (16, etag, "abc"))
        self.assertEqual(s3.meta_value(info.metadata["name"]), "Pokémon")
        self.assertEqual(self.client.get_bytes(k, 4, 7), b"4567")
        self.assertEqual(self.client.get_bytes(k, 10), b"abcdef")
        self.assertEqual([o.key for o in self.client.list("snes/")], [k])
        self.client.delete(k)
        self.assertIsNone(self.client.head(k))

    def test_a_file_is_streamed_up_and_down_and_never_lands_half(self) -> None:
        src = self.tmp / "game.7z"
        src.write_bytes(os.urandom(300_000))
        r = self.client.put_file(self.client.key("g.7z"), src, {"crc32": "1234abcd"})
        self.assertEqual((r["parts"], r["md5"], r["etag"]), (1, hashlib.md5(src.read_bytes()).hexdigest(), r["md5"]))
        dest = self.tmp / "back.7z"
        g = self.client.get_file(self.client.key("g.7z"), dest)
        self.assertEqual((dest.read_bytes(), g["md5"]), (src.read_bytes(), r["md5"]))
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ["back.7z", "game.7z"])        # (no .part left)
        with self.assertRaises(FileExistsError):
            self.client.get_file(self.client.key("g.7z"), dest)                                      # never over a file
        with self.assertRaises(s3.S3Error):
            self.client.get_file(self.client.key("nope"), self.tmp / "nope")
        self.assertFalse((self.tmp / "nope").exists() or (self.tmp / "nope.romorg.part").exists())

    def test_a_big_file_goes_in_parts_each_checked(self) -> None:
        client = self.make(multipart_threshold=5 << 20, part_size=5 << 20)
        src = self.tmp / "big.chd"
        src.write_bytes(os.urandom((11 << 20) + 123))
        r = client.put_file(client.key("big.chd"), src, {"sha1": "s"})
        self.assertEqual(r["parts"], 3)
        self.assertEqual(r["md5"], hashlib.md5(src.read_bytes()).hexdigest())
        self.assertEqual(self.fake.objects["arch/big.chd"], (src.read_bytes(), {"x-amz-meta-sha1": "s"}))
        self.assertEqual(client.list_uploads(), [])

    def test_a_failed_multipart_upload_is_aborted(self) -> None:
        client = self.make(multipart_threshold=5 << 20, part_size=5 << 20)
        src = self.tmp / "big.chd"
        src.write_bytes(os.urandom(6 << 20))
        real = s3.file_digests
        calls = []

        def bad(path, start=0, length=None, also=None):
            calls.append(1)
            md5, sha, n = real(path, start, length, also)
            return ("0" * 32 if len(calls) == 2 else md5), sha, n           # the second part's MD5 is wrong: the server refuses it
        s3.file_digests = bad
        try:
            with self.assertRaises(s3.S3Error) as cm:
                client.put_file(client.key("big.chd"), src)
        finally:
            s3.file_digests = real
        self.assertEqual(cm.exception.code, "BadDigest")
        self.assertEqual((self.fake.uploads, self.fake.objects), ({}, {}))     # aborted: nothing half there

    def test_a_body_that_does_not_match_its_md5_is_refused(self) -> None:
        real = s3._b64md5
        with self.assertRaises(s3.S3Error) as cm:
            s3._b64md5 = lambda h: real("0" * 32)                         # (the MD5 sent is not the body's)
            try:
                self.client.put_bytes(self.client.key("x"), b"data")
            finally:
                s3._b64md5 = real
        self.assertEqual(cm.exception.code, "BadDigest")
        self.assertEqual(self.fake.objects, {})

    def test_busy_answers_are_tried_again_and_others_are_not(self) -> None:
        _ = self.client.region
        self.fake.fail = [503, 500]
        self.client.put_bytes(self.client.key("a"), b"a")
        self.assertIn("arch/a", self.fake.objects)
        self.fake.fail = [503] * s3.RETRIES
        with self.assertRaises(s3.S3Error) as cm:
            self.client.put_bytes(self.client.key("b"), b"b")
        self.assertEqual(cm.exception.status, 503)
        bad = s3.Client(s3.Config(f"http://127.0.0.1:{self.srv.server_address[1]}", BUCKET, AK,
                                  "wrong", region=REGION), sleep=lambda s: None)
        n = len(self.fake.requests)
        with self.assertRaises(s3.S3Error) as cm:
            bad.put_bytes("c", b"c")
        self.assertEqual((cm.exception.code, len(self.fake.requests) - n), ("SignatureDoesNotMatch", 1))
        self.assertNotIn("wrong", str(cm.exception))

    def test_listing_follows_every_page_and_many_deletes_go_in_one_request(self) -> None:
        self.fake.page = 3
        keys = [self.client.key(f"n{i:02}") for i in range(8)]
        for k in keys:
            self.client.put_bytes(k, k.encode())
        self.assertEqual([o.key for o in self.client.list()], keys)
        n = len(self.fake.requests)
        self.assertEqual(self.client.delete_many(keys), [])
        self.assertEqual((self.fake.objects, len(self.fake.requests) - n), ({}, 1))

    def test_copy_keeps_or_replaces_the_metadata(self) -> None:
        a, b, c = self.client.key("a"), self.client.key("b"), self.client.key("c")
        self.client.put_bytes(a, b"x", {"sha1": "1"})
        self.client.copy(a, b)
        self.client.copy(a, c, {"sha1": "2"})
        self.assertEqual((self.client.head(b).metadata, self.client.head(c).metadata), ({"sha1": "1"}, {"sha1": "2"}))

    def test_the_probe(self) -> None:
        out = s3.probe(self.client)
        self.assertEqual(out, {"region": REGION, "listed": 0, "etag_is_md5": True, "metadata_back": True, "read_back": True,
                               "range_read": True, "deleted": True})
        self.assertEqual(self.fake.objects, {})


@unittest.skipUnless(os.environ.get("ROMORG_S4_TEST"), "set ROMORG_S4_TEST to a config file of a test bucket to try a real service")
class RealService(unittest.TestCase):
    def test_the_probe_against_the_real_service(self) -> None:
        out = s3.probe(s3.Client(s3.load_config(Path(os.environ["ROMORG_S4_TEST"]).expanduser())))
        self.assertTrue(all(v for k, v in out.items() if k not in ("region", "listed")), out)


if __name__ == "__main__":
    unittest.main()
