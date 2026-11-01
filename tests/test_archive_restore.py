"""Restoring from the archive (a folder, and an S3 bucket - the S3 server of test_s3): a recognised game goes in under its database
name, anything else back to the path it came from, the archive keeps its copy; the games the ROM folder lacks are found in the
archive by their checksums; a restored file archived again is not archived twice."""

from __future__ import annotations

import hashlib
import os
import sys
import threading
import unittest
from unittest import mock
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(__file__))

from pathlib import Path  # noqa: E402

from romorg import server  # noqa: E402

from servercase import GB, GBA, SNES, ServerCase, put, tree  # noqa: E402
from test_s3 import AK, BUCKET, SK, FakeS3, _handler  # noqa: E402

Q = GBA.replace(" ", "%20")


class LocalArchive(ServerCase):
    def setUp(self) -> None:
        super().setUp()
        self.root = self.tmp / "GBA"
        self.arch = self.tmp / "rom-archive" / "GBA"
        put(self.root / "run.gba", self.rom["Run (USA)"])
        put(self.arch / "Excluded" / "bash old.gba", self.rom["Bash (USA)"])           # archived by an earlier build
        put(self.arch / "Unmatched" / "notes" / "readme.txt", b"my notes")
        self.job("/api/scan", {"path": str(self.root), "platform": GBA})

    def test_a_missing_game_the_archive_holds_is_found_and_restored_under_its_name(self) -> None:
        top = self.call("GET", f"/api/tree?platform={Q}&side=library&show=missing")
        self.assertEqual(top["archive_restorable"], 1)
        bash = [f for f in top["files"] if f["name"] == "Bash (USA)"][0]
        self.assertEqual(bash["row"]["in_archive"], "Excluded/bash old.gba")
        res = self.job("/api/archive/restore", {"platform": GBA, "missing": True})
        self.assertEqual((res["restored"], res["placed"], res["failed"]), (1, 1, []))
        self.assertEqual(tree(self.root), ["Bash (USA).gba", "run.gba"])                       # (only what was restored is placed)
        self.assertEqual((self.root / "Bash (USA).gba").read_bytes(), self.rom["Bash (USA)"])
        self.assertTrue((self.arch / "Excluded" / "bash old.gba").is_file())                 # the archive keeps its copy

    def test_each_reason_has_a_view_of_its_folder_counted_as_the_folder_is(self) -> None:
        put(self.arch / "Excluded" / "more" / "two.gba", b"two")
        top = self.call("GET", f"/api/tree?platform={Q}&side=library&show=have")
        arch = self.call("GET", f"/api/tree?platform={Q}&side=archive")
        folder = {d["name"]: d["files"] for d in arch["dirs"]}
        self.assertEqual((top["sides"]["excluded"], top["sides"]["superseded"], top["sides"]["incomplete"]), (folder["Excluded"], 0, 0))
        self.assertEqual(folder["Excluded"], 2)
        view = self.call("GET", f"/api/tree?platform={Q}&side=archive&show=excluded&path=Excluded")
        self.assertEqual(([d["name"] for d in view["dirs"]], [f["name"] for f in view["files"]]), (["more"], ["bash old.gba"]))
        none = self.call("GET", f"/api/tree?platform={Q}&side=archive&show=superseded&path=Superseded")      # (nothing set aside: an empty view)
        self.assertEqual((none["dirs"], none["files"]), ([], []))
        self.assertEqual(self.call("GET", f"/api/tree?platform={Q}&side=archive&show=superseded")["dirs"], [])

    def test_the_preview_is_worked_out_once_until_something_it_looks_at_changes(self) -> None:
        body = {"summary": True, "limit": 1, "aside_to": str(self.tmp / "rom-archive")}
        first = self.call("POST", "/api/library/plan", body)
        with mock.patch.object(server.App, "_all_archive_moves", side_effect=AssertionError("worked out again")):
            self.assertEqual(self.call("POST", "/api/library/plan", body)["plan_id"], first["plan_id"])
            self.call("GET", f"/api/tree?platform={Q}&side=library&show=have")               # (Files, and back)
            self.assertEqual(self.call("POST", "/api/library/plan", body)["files"], first["files"])
        self.call("POST", "/api/library/profile", {"platform": GBA, "override": {"fields": ["keep_bios"], "on": True}})
        self.call("POST", "/api/library/profile", {"platform": GBA, "keep_bios": False})       # (new rules: a new preview)
        with mock.patch.object(server.App, "_all_archive_moves", return_value=[]) as again:
            self.call("POST", "/api/library/plan", body)
        again.assert_called_once()

    def test_an_unrecognised_file_goes_back_where_it_came_from_and_is_not_archived_twice(self) -> None:
        res = self.job("/api/archive/restore", {"platform": GBA, "paths": ["Unmatched/notes"]})
        self.assertEqual((res["restored"], res["placed"], res["at_original"]), (1, 0, 1))
        self.assertEqual((self.root / "notes" / "readme.txt").read_bytes(), b"my notes")
        self.job("/api/scan", {"path": str(self.root), "platform": GBA})
        aside = str(self.tmp / "rom-archive")
        self.job("/api/library/apply", {"aside_to": aside})
        self.assertFalse((self.root / "notes").exists())
        self.assertEqual(sorted(p.name for p in (self.arch / "Unmatched" / "notes").iterdir()), ["readme.txt"])   # (no "readme (2).txt")

    def test_a_file_an_earlier_build_archived_under_the_old_name_is_not_archived_twice(self) -> None:
        put(self.arch / "_unmatched" / "old" / "readme.txt", b"older notes")                 # (an archive from before the plain names)
        self.job("/api/archive/restore", {"platform": GBA, "paths": ["_unmatched/old"]})
        self.assertEqual((self.root / "old" / "readme.txt").read_bytes(), b"older notes")
        self.job("/api/scan", {"path": str(self.root), "platform": GBA})
        self.job("/api/library/apply", {"aside_to": str(self.tmp / "rom-archive")})
        self.assertFalse((self.root / "old").exists())
        self.assertTrue((self.arch / "_unmatched" / "old" / "readme.txt").is_file())      # (it stays where it was)
        self.assertFalse((self.arch / "Unmatched" / "old").exists())                      # ... and is not archived a second time

    def test_a_restored_excluded_game_is_not_archived_a_second_time_under_its_database_name(self) -> None:
        self.job("/api/archive/restore", {"platform": GBA, "paths": ["Excluded/bash old.gba"]})
        self.assertTrue((self.root / "Bash (USA).gba").is_file())                          # (restored under the database's name)
        self.job("/api/scan", {"path": str(self.root), "platform": GBA})
        self.call("POST", "/api/library/override", {"action": "exclude", "games": [{"dat": "Nintendo - Game Boy Advance", "game": "Bash (USA)"}]})
        res = self.job("/api/library/apply", {"aside_to": str(self.tmp / "rom-archive")})
        self.assertFalse((self.root / "Bash (USA).gba").exists())
        self.assertEqual(sorted(p.name for p in (self.arch / "Excluded").iterdir()), ["bash old.gba"])     # (no second copy)
        self.assertEqual(res["aside"]["failed"], [])

    def test_nothing_chosen_is_refused(self) -> None:
        self.assertEqual(self.http("POST", "/api/archive/restore", {"platform": GBA, "paths": []})[0], 400)


class ArchiveFolderNames(ServerCase):
    """The archive folder of a system: unambiguous, and an archive an older version made under the ROM folder's name is still used."""

    def _names(self, folders: dict[str, str]) -> dict[str, str]:
        app = self.srv.app
        for plat, folder in folders.items():
            app._config_update(folder_for=(plat, folder))
        aside = self.tmp / "rom-archive"
        return {plat: app._archive_name(app._resolve_platform(plat), Path(folder), aside) for plat, folder in folders.items()}

    def test_a_folder_name_that_says_nothing_or_that_two_systems_share_is_named_after_the_platform(self) -> None:
        got = self._names({GBA: str(self.tmp / "GBA"), SNES: str(self.tmp / "a" / "games"), GB: str(self.tmp / "b" / "games")})
        self.assertEqual(got[GBA], "GBA")                                                     # (unique and clear: as before)
        self.assertEqual(got[SNES], SNES)                                                     # (two systems, one name: never one folder)
        self.assertEqual(got[GB], GB)
        self.assertEqual(self._names({SNES: str(self.tmp / "a" / "games"), GB: str(self.tmp / "b" / "x")})[SNES], SNES)   # (a name that says nothing)

    def test_an_archive_an_older_version_made_under_the_folder_name_is_still_used(self) -> None:
        (self.tmp / "rom-archive" / "games").mkdir(parents=True)
        self.assertEqual(self._names({SNES: str(self.tmp / "games")})[SNES], "games")
        (self.tmp / "rom-archive" / SNES).mkdir(parents=True)                               # (a folder of its own is there too: that one)
        self.assertEqual(self._names({SNES: str(self.tmp / "games")})[SNES], SNES)

    def test_two_systems_with_one_folder_name_archive_to_two_folders(self) -> None:
        a, b = self.tmp / "a" / "games", self.tmp / "b" / "games"
        put(a / "run.gba", self.rom["Run (USA)"])
        put(a / "what.gba", b"unknown a")
        put(b / "alpha.sfc", self.rom["Alpha (USA)"])
        put(b / "what.sfc", b"unknown b")
        aside = self.tmp / "rom-archive"
        self._names({GBA: str(a), SNES: str(b)})
        for plat, folder in ((GBA, a), (SNES, b)):
            self.job("/api/scan", {"path": str(folder), "platform": plat})
            self.job("/api/library/apply", {"aside_to": str(aside)})
        self.assertEqual([x for x in tree(aside) if not x.endswith("/")], [f"{GBA}/Unmatched/what.gba", f"{SNES}/Unmatched/what.sfc"])


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
        self.fake.objects["arch/GBA/Excluded/bash old.gba"] = (data, {                       # (uploaded by another run: read with a HEAD)
            "x-amz-meta-sha1": hashlib.sha1(data).hexdigest(), "x-amz-meta-md5": hashlib.md5(data).hexdigest(), "x-amz-meta-from": "old/bash.gba"})
        self.fake.objects["arch/GBA/Unmatched/readme.txt"] = (b"my notes", {})
        self.job("/api/scan", {"path": str(self.root), "platform": GBA})

    def test_tidying_moves_objects_inside_the_bucket_and_keeps_their_metadata(self) -> None:
        from romorg import organiser

        with mock.patch.object(organiser, "tidy_archive_parts", side_effect=lambda p, parts: ("Excluded", "Tidy", "bash old.gba") if tuple(parts) == ("Excluded", "bash old.gba") else None):
            res = self.job("/api/archive/tidy", {"platform": GBA})
        self.assertEqual((res["moved"], res["failed"]), (1, []))
        self.assertNotIn("arch/GBA/Excluded/bash old.gba", self.fake.objects)
        data, meta = self.fake.objects["arch/GBA/Excluded/Tidy/bash old.gba"]
        self.assertEqual(data, self.rom["Bash (USA)"])
        self.assertEqual(meta.get("x-amz-meta-from"), "old/bash.gba")                       # (where restore puts it back: kept)
        self.assertIn("arch/GBA/Unmatched/readme.txt", self.fake.objects)
        files = self.call("GET", f"/api/tree?platform={Q}&side=archive&path=Excluded%2FTidy")["files"]
        self.assertEqual([f["name"] for f in files], ["bash old.gba"])                       # (the tree follows)

    def test_a_missing_game_in_the_bucket_is_found_by_its_metadata_and_restored(self) -> None:
        top = self.call("GET", f"/api/tree?platform={Q}&side=library&show=missing")
        self.assertEqual(top["archive_restorable"], 1)
        heads = sum(1 for r in self.fake.requests if r.startswith("HEAD "))
        res = self.job("/api/archive/restore", {"platform": GBA, "missing": True})
        self.assertEqual((res["restored"], res["placed"], res["failed"]), (1, 1, []))
        self.assertEqual((self.root / "Bash (USA).gba").read_bytes(), self.rom["Bash (USA)"])
        self.assertIn("arch/GBA/Excluded/bash old.gba", self.fake.objects)                      # the bucket keeps it
        self.assertFalse(list(self.root.rglob("*.romorg.part")))
        self.assertEqual(sum(1 for r in self.fake.requests if r.startswith("HEAD ")), heads)      # (its metadata was kept from the scan)

    def test_several_chosen_files_and_a_restored_file_archived_again_is_not_uploaded_twice(self) -> None:
        res = self.job("/api/archive/restore", {"platform": GBA, "paths": ["Excluded/bash old.gba", "Unmatched/readme.txt"]})
        self.assertEqual((res["restored"], res["placed"], res["at_original"]), (2, 1, 1))
        self.assertEqual((self.root / "readme.txt").read_bytes(), b"my notes")                    # (no "from": its path in the archive)
        self.job("/api/scan", {"path": str(self.root), "platform": GBA})
        before = len(self.fake.objects)
        res = self.job("/api/library/apply", {"aside_to": f"s3://{BUCKET}/arch"})
        self.assertEqual(res["aside"]["failed"], [])
        self.assertFalse((self.root / "readme.txt").exists())
        self.assertEqual(len(self.fake.objects), before)                                           # (the same object: no "readme (2).txt")

    def test_a_restored_excluded_game_is_not_uploaded_a_second_time_under_its_database_name(self) -> None:
        self.job("/api/archive/restore", {"platform": GBA, "paths": ["Excluded/bash old.gba"]})
        self.job("/api/scan", {"path": str(self.root), "platform": GBA})
        self.call("POST", "/api/library/override", {"action": "exclude", "games": [{"dat": "Nintendo - Game Boy Advance", "game": "Bash (USA)"}]})
        before = sorted(self.fake.objects)
        res = self.job("/api/library/apply", {"aside_to": f"s3://{BUCKET}/arch"})
        self.assertEqual(res["aside"]["failed"], [])
        self.assertFalse((self.root / "Bash (USA).gba").exists())
        self.assertEqual(sorted(self.fake.objects), before)                                        # (the object under the source's name is the one held)

    def test_a_file_an_earlier_build_archived_under_the_old_name_is_not_uploaded_twice(self) -> None:
        del self.fake.objects["arch/GBA/Unmatched/readme.txt"]
        self.fake.objects["arch/GBA/_unmatched/readme.txt"] = (b"my notes", {})                 # (uploaded before the plain names)
        self.job("/api/scan", {"path": str(self.root), "platform": GBA})
        self.job("/api/archive/restore", {"platform": GBA, "paths": ["_unmatched/readme.txt"]})
        self.job("/api/scan", {"path": str(self.root), "platform": GBA})
        before = sorted(self.fake.objects)
        res = self.job("/api/library/apply", {"aside_to": f"s3://{BUCKET}/arch"})
        self.assertEqual(res["aside"]["failed"], [])
        self.assertFalse((self.root / "readme.txt").exists())
        self.assertEqual(sorted(self.fake.objects), before)                                        # (the old-named object is the one that is held)


if __name__ == "__main__":
    unittest.main()
