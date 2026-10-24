"""Restoring from the archive (a folder, and an S3 bucket - the S3 server of test_s3): a recognised game goes in under its database
name, anything else back to the path it came from, the archive keeps its copy; the games the ROM folder lacks are found in the
archive by their checksums; a restored file archived again is not archived twice."""

from __future__ import annotations

import hashlib
import os
import sys
import threading
import unittest
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(__file__))

from servercase import GBA, ServerCase, put, tree  # noqa: E402
from test_s3 import AK, BUCKET, SK, FakeS3, _handler  # noqa: E402

Q = GBA.replace(" ", "%20")


class LocalArchive(ServerCase):
    def setUp(self) -> None:
        super().setUp()
        self.root = self.tmp / "GBA"
        self.arch = self.tmp / "rom-archive" / "GBA"
        put(self.root / "run.gba", self.rom["Run (USA)"])
        put(self.arch / "_excluded" / "bash old.gba", self.rom["Bash (USA)"])           # archived by an earlier build
        put(self.arch / "_unmatched" / "notes" / "readme.txt", b"my notes")
        self.job("/api/scan", {"path": str(self.root), "platform": GBA})

    def test_a_missing_game_the_archive_holds_is_found_and_restored_under_its_name(self) -> None:
        top = self.call("GET", f"/api/tree?platform={Q}&side=library&show=missing")
        self.assertEqual(top["archive_restorable"], 1)
        bash = [f for f in top["files"] if f["name"] == "Bash (USA)"][0]
        self.assertEqual(bash["row"]["in_archive"], "_excluded/bash old.gba")
        res = self.job("/api/archive/restore", {"platform": GBA, "missing": True})
        self.assertEqual((res["restored"], res["placed"], res["failed"]), (1, 1, []))
        self.assertEqual(tree(self.root), ["Bash (USA).gba", "run.gba"])                       # (only what was restored is placed)
        self.assertEqual((self.root / "Bash (USA).gba").read_bytes(), self.rom["Bash (USA)"])
        self.assertTrue((self.arch / "_excluded" / "bash old.gba").is_file())                 # the archive keeps its copy

    def test_an_unrecognised_file_goes_back_where_it_came_from_and_is_not_archived_twice(self) -> None:
        res = self.job("/api/archive/restore", {"platform": GBA, "paths": ["_unmatched/notes"]})
        self.assertEqual((res["restored"], res["placed"], res["at_original"]), (1, 0, 1))
        self.assertEqual((self.root / "notes" / "readme.txt").read_bytes(), b"my notes")
        self.job("/api/scan", {"path": str(self.root), "platform": GBA})
        aside = str(self.tmp / "rom-archive")
        self.job("/api/library/apply", {"aside_to": aside})
        self.assertFalse((self.root / "notes").exists())
        self.assertEqual(sorted(p.name for p in (self.arch / "_unmatched" / "notes").iterdir()), ["readme.txt"])   # (no "readme (2).txt")

    def test_nothing_chosen_is_refused(self) -> None:
        self.assertEqual(self.http("POST", "/api/archive/restore", {"platform": GBA, "paths": []})[0], 400)


class S3Restore(ServerCase):
    def setUp(self) -> None:
        super().setUp()
        self.fake = FakeS3()
        self.s3srv = ThreadingHTTPServer(("127.0.0.1", 0), _handler(self.fake))
        threading.Thread(target=self.s3srv.serve_forever, daemon=True).start()
        self.addCleanup(self.s3srv.server_close)
        self.addCleanup(self.s3srv.shutdown)
        self.call("POST", "/api/settings/s3", {"endpoint": f"http://127.0.0.1:{self.s3srv.server_address[1]}", "access_key": AK,
                                               "secret_key": SK})
        self.call("POST", "/api/settings/archive", {"dir": f"s3://{BUCKET}/arch"})
        self.root = self.tmp / "GBA"
        put(self.root / "run.gba", self.rom["Run (USA)"])
        data = self.rom["Bash (USA)"]
        self.fake.objects["arch/GBA/_excluded/bash old.gba"] = (data, {                       # (uploaded by another run: read with a HEAD)
            "x-amz-meta-sha1": hashlib.sha1(data).hexdigest(), "x-amz-meta-md5": hashlib.md5(data).hexdigest(), "x-amz-meta-from": "old/bash.gba"})
        self.fake.objects["arch/GBA/_unmatched/readme.txt"] = (b"my notes", {})
        self.job("/api/scan", {"path": str(self.root), "platform": GBA})

    def test_a_missing_game_in_the_bucket_is_found_by_its_metadata_and_restored(self) -> None:
        top = self.call("GET", f"/api/tree?platform={Q}&side=library&show=missing")
        self.assertEqual(top["archive_restorable"], 1)
        heads = sum(1 for r in self.fake.requests if r.startswith("HEAD "))
        res = self.job("/api/archive/restore", {"platform": GBA, "missing": True})
        self.assertEqual((res["restored"], res["placed"], res["failed"]), (1, 1, []))
        self.assertEqual((self.root / "Bash (USA).gba").read_bytes(), self.rom["Bash (USA)"])
        self.assertIn("arch/GBA/_excluded/bash old.gba", self.fake.objects)                      # the bucket keeps it
        self.assertFalse(list(self.root.rglob("*.romorg.part")))
        self.assertEqual(sum(1 for r in self.fake.requests if r.startswith("HEAD ")), heads)      # (its metadata was kept from the scan)

    def test_several_chosen_files_and_a_restored_file_archived_again_is_not_uploaded_twice(self) -> None:
        res = self.job("/api/archive/restore", {"platform": GBA, "paths": ["_excluded/bash old.gba", "_unmatched/readme.txt"]})
        self.assertEqual((res["restored"], res["placed"], res["at_original"]), (2, 1, 1))
        self.assertEqual((self.root / "readme.txt").read_bytes(), b"my notes")                    # (no "from": its path in the archive)
        self.job("/api/scan", {"path": str(self.root), "platform": GBA})
        before = len(self.fake.objects)
        res = self.job("/api/library/apply", {"aside_to": f"s3://{BUCKET}/arch"})
        self.assertEqual(res["aside"]["failed"], [])
        self.assertFalse((self.root / "readme.txt").exists())
        self.assertEqual(len(self.fake.objects), before)                                           # (the same object: no "readme (2).txt")


if __name__ == "__main__":
    unittest.main()
