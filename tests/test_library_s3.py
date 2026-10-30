"""Build library with the archive in an S3 bucket (an S3 server in the tests, see test_s3): what the rules set aside is uploaded,
checked and deleted from the disk; the file tree's Archive side is the bucket's listing; the S3 settings never hand out the secret."""

from __future__ import annotations

import os
import stat
import sys
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))

from servercase import GBA, ServerCase, put, tree  # noqa: E402
from test_s3 import AK, BUCKET, SK, FakeS3, _handler  # noqa: E402

ARCH = f"s3://{BUCKET}/arch"


class S3Archive(ServerCase):
    def setUp(self) -> None:
        super().setUp()
        self.fake = FakeS3()
        self.s3srv = ThreadingHTTPServer(("127.0.0.1", 0), _handler(self.fake))
        threading.Thread(target=self.s3srv.serve_forever, daemon=True).start()
        self.addCleanup(self.s3srv.server_close)
        self.addCleanup(self.s3srv.shutdown)
        self.call("POST", "/api/settings/s3", {"endpoint": f"http://127.0.0.1:{self.s3srv.server_address[1]}", "access_key": AK,
                                               "secret_key": SK})
        self.call("POST", "/api/settings/archive", {"dir": ARCH + "/"})
        self.root = self.tmp / "GBA"

    def world(self) -> None:
        put(self.root / "run.gba", self.rom["Run (USA)"])
        put(self.root / "run copy.gba", self.rom["Run (USA)"])                         # a duplicate
        put(self.root / "_excluded" / "old leftover.gba", b"left by an earlier build")   # swept out
        put(self.root / "what.gba", b"unknown")                                          # unmatched
        self.job("/api/scan", {"path": str(self.root), "platform": GBA})

    def archive_tree(self) -> dict:
        return self.call("GET", f"/api/tree?platform={GBA.replace(' ', '%20')}&side=archive&show=")

    def test_the_secret_is_kept_apart_and_never_handed_out(self) -> None:
        got = self.call("GET", "/api/settings/s3")
        self.assertEqual((got["access_key"], got["secret_set"]), (AK, True))
        self.assertNotIn(SK, str(got) + str(self.call("GET", "/api/status")))
        secret = self.tmp / "data" / "s3-secret"
        self.assertEqual((secret.read_text(), stat.S_IMODE(secret.stat().st_mode) if os.name == "posix" else 0o600), (SK, 0o600))
        self.assertNotIn(SK, (self.tmp / "data" / "config.json").read_text())
        self.call("POST", "/api/settings/s3", {"endpoint": "s3.example", "access_key": "other"})       # no secret given: the stored one stays
        self.assertEqual(secret.read_text(), SK)
        self.assertEqual(self.call("GET", "/api/status")["archive"]["dir"], ARCH + "/")

    def test_the_connection_test(self) -> None:
        out = self.call("POST", "/api/settings/s3/test", {"location": ARCH})
        self.assertTrue(out["ok"], out)
        self.assertEqual(self.fake.objects, {})
        self.assertEqual(self.http("POST", "/api/settings/s3/test", {"location": "/a/folder"})[0], 400)

    def test_a_build_uploads_what_it_sets_aside_checks_it_and_bins_the_local_copy(self) -> None:
        self.world()
        plan = self.call("POST", "/api/library/plan", {"limit": 50, "aside_to": ARCH})
        self.assertEqual((plan["aside"]["path"], plan["aside"]["s3"]), (f"{ARCH}/", True))
        before = self.archive_tree()
        self.assertEqual((before["root"], before["archive_s3"]["url"], before["archive_s3"]["error"]), (f"{ARCH}/GBA/", f"{ARCH}/GBA/", ""))
        self.assertEqual(sorted(d["name"] for d in before["dirs"]), ["Duplicates", "Excluded", "Unmatched"])
        res = self.job("/api/library/apply", {"aside_to": ARCH, "plan_id": plan["plan_id"]})
        self.assertEqual((res["aside"]["moved"], res["aside"]["failed"], res["aside"]["s3"]), (3, [], True))
        self.assertEqual(tree(self.root), ["Run (USA).gba"])                                       # a clean library: nothing else, no folder
        self.assertEqual(sorted(self.fake.objects), ["arch/GBA/Duplicates/run copy.gba", "arch/GBA/Excluded/old leftover.gba",
                                                     "arch/GBA/Unmatched/what.gba"])
        data, meta = self.fake.objects["arch/GBA/Unmatched/what.gba"]
        self.assertEqual((data, meta["x-amz-meta-from"], meta["x-amz-meta-size"]), (b"unknown", "what.gba", "7"))
        self.assertEqual(sorted(meta), ["x-amz-meta-crc32", "x-amz-meta-from", "x-amz-meta-md5", "x-amz-meta-sha1", "x-amz-meta-size"])
        self.assertFalse((self.tmp / "data" / "s3-archive").exists())                              # the stand-in folder is never written
        after = self.archive_tree()
        self.assertEqual(after["totals"]["files"], 3)
        ex = self.call("GET", f"/api/tree?platform={GBA.replace(' ', '%20')}&side=archive&show=&path=Unmatched")
        self.assertEqual([(f["name"], f["state"]) for f in ex["files"]], [("what.gba", "existing")])

    def test_a_name_another_object_has_gets_a_number(self) -> None:
        self.fake.objects["arch/GBA/Unmatched/what.gba"] = (b"something else", {})
        self.world()
        res = self.job("/api/library/apply", {"aside_to": ARCH})
        self.assertEqual(res["aside"]["failed"], [])
        self.assertEqual(self.fake.objects["arch/GBA/Unmatched/what.gba"][0], b"something else")    # never replaced
        self.assertEqual(self.fake.objects["arch/GBA/Unmatched/what (2).gba"][0], b"unknown")

    def test_a_service_that_cannot_be_reached_stops_the_build_before_anything_moves(self) -> None:
        self.world()
        self.fake.fail = [503] * 50
        self.call("POST", "/api/library/apply", {"aside_to": ARCH})
        job = self.wait()
        self.assertEqual(job["status"], "error", job)
        self.assertIn("cannot be reached", job["error"])
        self.assertTrue((self.root / "run copy.gba").is_file() and (self.root / "what.gba").is_file())

    def test_without_a_connection_the_build_is_refused(self) -> None:
        self.world()
        self.call("POST", "/api/settings/s3", {"clear": True})
        code, out = self.http("POST", "/api/library/apply", {"aside_to": ARCH})
        self.assertEqual((code, out.get("code")), (409, "no_s3"))
        self.assertIn("not set up", self.archive_tree()["archive_s3"]["error"])

    def test_a_save_of_two_games_is_uploaded_once_and_copied_by_the_service(self) -> None:
        from test_memcards import vmu_card
        put(self.root / "Run (USA).gba", self.rom["Run (USA)"])
        card = vmu_card([("RUN.001", "", "Run"), ("BASH.001", "", "Bash")])
        put(self.root / "card.bin", card)
        self.job("/api/scan", {"path": str(self.root), "platform": GBA})
        res = self.job("/api/library/apply", {"aside_to": ARCH})
        self.assertEqual(res["aside"]["failed"], [])
        self.assertEqual(self.fake.objects["arch/GBA/_saves/Run/card.bin"][0], card)
        self.assertEqual(self.fake.objects["arch/GBA/_saves/Bash/card.bin"][0], card)                # (a copy made by the service)
        self.assertEqual(tree(self.root), ["Run (USA).gba"])


@unittest.skipUnless(os.environ.get("ROMORG_S4_TEST"), "set ROMORG_S4_TEST to a config file of a test bucket to build against a real service")
class RealService(ServerCase):
    def test_a_build_archives_to_the_real_bucket(self) -> None:
        import uuid
        from romorg import s3
        cfg = s3.load_config(Path(os.environ["ROMORG_S4_TEST"]).expanduser())
        prefix = f"romorg-test-{uuid.uuid4().hex[:10]}"
        client = s3.Client(s3.Config(cfg.endpoint if "://" in cfg.endpoint else cfg.host, cfg.bucket, cfg.access_key, cfg.secret_key,
                                     cfg.region, prefix))
        self.addCleanup(lambda: client.delete_many([o.key for o in client.list()]))
        self.call("POST", "/api/settings/s3", {"endpoint": cfg.host, "region": cfg.region, "access_key": cfg.access_key,
                                               "secret_key": cfg.secret_key})
        where = f"s3://{cfg.bucket}/{prefix}"
        self.call("POST", "/api/settings/archive", {"dir": where})
        root = self.tmp / "GBA"
        put(root / "run.gba", self.rom["Run (USA)"])
        put(root / "run copy.gba", self.rom["Run (USA)"])
        put(root / "what.gba", b"unknown")
        self.job("/api/scan", {"path": str(root), "platform": GBA})
        res = self.job("/api/library/apply", {"aside_to": where})
        self.assertEqual((res["aside"]["moved"], res["aside"]["failed"]), (2, []))
        self.assertEqual(tree(root), ["Run (USA).gba"])
        objs = {o.key: o for o in client.list()}
        self.assertEqual(sorted(objs), [f"{prefix}/GBA/_duplicates/run copy.gba", f"{prefix}/GBA/_unmatched/what.gba"])
        info = client.head(f"{prefix}/GBA/_unmatched/what.gba")
        self.assertEqual((info.size, info.md5, info.metadata["from"]), (7, __import__("hashlib").md5(b"unknown").hexdigest(), "what.gba"))
        side = self.call("GET", f"/api/tree?platform={GBA.replace(' ', '%20')}&side=archive&show=")
        self.assertEqual((side["root"], side["totals"]["files"]), (f"{where}/GBA/", 2))
        back = self.job("/api/archive/restore", {"platform": GBA, "paths": ["_unmatched/what.gba"]})
        self.assertEqual((back["restored"], back["at_original"], back["failed"]), (1, 1, []))
        self.assertEqual((root / "what.gba").read_bytes(), b"unknown")                   # back where it came from (its "from")
        self.assertIsNotNone(client.head(f"{prefix}/GBA/_unmatched/what.gba"))            # the bucket keeps it


if __name__ == "__main__":
    unittest.main()
