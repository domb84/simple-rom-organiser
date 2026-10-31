"""The upload path of the S3 archive (romorg.s3archive.upload_moves) against the S3 server of the tests: what is sent, how an upload
is checked before the local file goes, and what a failure or a cancel leaves behind."""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))

from romorg import s3, s3archive, sortroot  # noqa: E402
from test_s3 import AK, BUCKET, REGION, SK, FakeS3, _handler  # noqa: E402


class UploadMoves(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeS3()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), _handler(self.fake))
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.tmp = Path(tempfile.mkdtemp(prefix="romorg-s3up-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.target = s3archive.Target(BUCKET, "arch/")
        self.base = self.tmp / "standin"
        (self.tmp / "roms").mkdir()
        self.client = self.make()
        self.inv = s3archive.Inventory(self.client, self.target, self.base)

    def make(self, **kw) -> s3.Client:
        cfg = s3.Config(f"http://127.0.0.1:{self.srv.server_address[1]}", BUCKET, AK, SK, region=REGION, prefix="arch")
        c = s3.Client(cfg, sleep=lambda s: None, **kw)
        self.addCleanup(c.close)
        return c

    def files(self, n: int, size: int = 3000, prefix: str = "f") -> list[sortroot.SMove]:
        moves = []
        for i in range(n):
            p = self.tmp / "roms" / f"{prefix}{i:03}.bin"
            p.write_bytes(os.urandom(size) + str(i).encode())
            moves.append(sortroot.SMove(p, self.base / "snes" / p.name, "snes", "", "file", p.stat().st_size))
        return moves

    def count(self, method: str) -> int:
        return sum(1 for r in self.fake.requests if r.startswith(method + " "))

    def test_small_files_are_one_request_each_over_kept_open_connections_and_the_local_file_goes(self) -> None:
        moves = self.files(40)
        data = {m.dst.name: Path(m.src).read_bytes() for m in moves}
        res = s3archive.upload_moves(self.client, self.inv, moves, workers=4)
        self.assertEqual((res["moved"], res["uploaded"], res["failed"], res["cancelled"]), (40, 40, [], False))
        self.assertEqual((self.count("PUT"), self.count("HEAD")), (40, 0))                 # no HEAD to check: the PUT's answer is the check
        self.assertLessEqual(self.fake.connections, 4 + 2)                                  # (a handshake per worker, not per request)
        for name, body in data.items():
            stored, meta = self.fake.objects[f"arch/snes/{name}"]
            self.assertEqual((stored, meta["x-amz-meta-sha1"], meta["x-amz-meta-md5"]),
                             (body, hashlib.sha1(body).hexdigest(), hashlib.md5(body).hexdigest()))
        self.assertFalse((self.tmp / "roms").exists())                                      # (the files of a move are gone, and the emptied folder)
        info = self.inv.get("arch/snes/f000.bin")                                           # (and the inventory knows them, with metadata)
        self.assertEqual((info.size, info.metadata["sha1"]), (len(data["f000.bin"]), hashlib.sha1(data["f000.bin"]).hexdigest()))

    def test_verify_head_reads_every_object_back(self) -> None:
        moves = self.files(5)
        res = s3archive.upload_moves(self.client, self.inv, moves, workers=2, verify="head")
        self.assertEqual((res["moved"], self.count("HEAD")), (5, 5))

    def test_a_service_whose_etag_is_not_the_md5_is_read_back(self) -> None:
        moves = self.files(3)
        real = self.client.put_file

        def put(*a, **k):
            r = real(*a, **k)
            r["verified"] = False                                   # (an ETag that says nothing about the bytes)
            return r
        self.client.put_file = put                                 # type: ignore[method-assign]
        res = s3archive.upload_moves(self.client, self.inv, moves, workers=2)
        self.assertEqual((res["moved"], self.count("HEAD")), (3, 3))

    def test_a_big_file_goes_in_parallel_parts_and_is_checked_by_one_head(self) -> None:
        client = self.make(multipart_threshold=5 << 20, part_size=5 << 20, parts=3)
        inv = s3archive.Inventory(client, self.target, self.base)
        self.fake.part_delay = 0.05
        p = self.tmp / "roms" / "big.chd"
        p.write_bytes(os.urandom((5 << 20) * 3 + 99))
        body = p.read_bytes()
        res = s3archive.upload_moves(client, inv, [sortroot.SMove(p, self.base / "ps2" / "big.chd", "ps2")], workers=2)
        self.assertEqual((res["moved"], res["failed"], self.count("HEAD"), self.fake.parts_max), (1, [], 1, 3))
        self.assertEqual(self.fake.objects["arch/ps2/big.chd"][0], body)
        self.assertFalse(p.exists())
        self.assertEqual((self.fake.uploads, client.list_uploads()), ({}, []))

    def test_a_refused_upload_keeps_the_local_file(self) -> None:
        moves = self.files(3)
        self.inv.objects("snes")                                    # (listed already)
        self.fake.fail = [403]                                      # the next request is refused for good
        res = s3archive.upload_moves(self.client, self.inv, moves, workers=1)
        self.assertEqual((res["moved"], len(res["failed"])), (2, 1))
        left = [p.name for p in (self.tmp / "roms").iterdir()]
        self.assertEqual(len(left), 1)
        self.assertEqual(Path(res["failed"][0]["path"]).name, left[0])
        self.assertEqual(len(self.fake.objects), 2)

    def test_a_file_that_changes_during_the_upload_is_not_deleted(self) -> None:
        moves = self.files(1)
        real = self.client.put_file

        def put(key, path, *a, **k):
            r = real(key, path, *a, **k)
            Path(path).write_bytes(b"changed meanwhile")
            return r
        self.client.put_file = put                                 # type: ignore[method-assign]
        res = s3archive.upload_moves(self.client, self.inv, moves, workers=1)
        self.assertEqual((res["moved"], len(res["failed"])), (0, 1))
        self.assertIn("changed", res["failed"][0]["error"])
        self.assertTrue(Path(moves[0].src).exists())

    def test_a_cancel_stops_the_files_not_started_and_leaves_their_local_files(self) -> None:
        moves = self.files(30)
        asked = []

        def cancel() -> bool:
            asked.append(1)
            return len(asked) > 12
        res = s3archive.upload_moves(self.client, self.inv, moves, workers=2, cancel=cancel)
        self.assertTrue(res["cancelled"])
        self.assertLess(res["moved"], 30)
        self.assertEqual(res["failed"], [])
        self.assertEqual(len(list((self.tmp / "roms").iterdir())) + res["moved"], 30)       # (nothing lost: uploaded or still there)
        self.assertEqual(len(self.fake.objects), res["moved"])

    def test_a_cancel_stops_a_big_file_between_parts_and_aborts_it(self) -> None:
        client = self.make(multipart_threshold=5 << 20, part_size=5 << 20, parts=1)
        inv = s3archive.Inventory(client, self.target, self.base)
        p = self.tmp / "roms" / "big.chd"
        p.write_bytes(os.urandom(16 << 20))
        asked = []

        def cancel() -> bool:
            asked.append(1)
            return len(asked) > 6
        res = s3archive.upload_moves(client, inv, [sortroot.SMove(p, self.base / "ps2" / "big.chd", "ps2")], workers=1, cancel=cancel)
        self.assertEqual((res["cancelled"], res["moved"], res["failed"]), (True, 0, []))
        self.assertTrue(p.exists())
        self.assertEqual((self.fake.uploads, self.fake.objects), ({}, {}))

    def test_the_biggest_files_start_first(self) -> None:
        small = self.files(3, 100, "s")
        big = self.files(1, 400_000, "b")
        s3archive.upload_moves(self.client, self.inv, small + big, workers=1)
        puts = [r for r in self.fake.requests if r.startswith("PUT ")]
        self.assertIn("b000", puts[0])

    def test_the_number_of_uploads_at_once_is_a_setting(self) -> None:
        self.assertEqual(s3archive.default_workers(), s3archive.WORKERS)
        self.assertEqual(s3archive.default_workers(12), 12)
        self.assertEqual(s3archive.default_workers(500), 64)
        self.assertEqual(s3archive.default_workers("x"), s3archive.WORKERS)
        os.environ[s3archive.ENV_WORKERS] = "3"
        try:
            self.assertEqual(s3archive.default_workers(12), 3)
        finally:
            del os.environ[s3archive.ENV_WORKERS]


if __name__ == "__main__":
    unittest.main()
