"""The user's own IGDB key: the index it makes, how it joins LaunchBox's, and the Settings endpoints (no network: IGDB is faked)."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from tests.servercase import ServerCase  # noqa: E402  (sets ROMORG_RETROARCH_DETECT=0 first)

from romorg import igdb, platforms, ratings, tosec


class Resp:
    def __init__(self, doc) -> None:
        self.doc = doc

    def read(self) -> bytes:
        return json.dumps(self.doc).encode()

    def close(self) -> None:
        pass


class FakeIgdb:
    """Twitch + IGDB: a token for the right key, and rated games per platform id, 500 to a page."""

    def __init__(self, per_platform: dict[int, list[dict]], secret: str = "s3cret") -> None:
        self.games = per_platform
        self.secret = secret
        self.requests: list[tuple[str, str]] = []
        self.limit_once = False

    def __call__(self, req):
        url = req.full_url
        if url.startswith(igdb.TOKEN_URL):
            self.requests.append(("token", url))
            if f"client_secret={self.secret}" not in url:
                raise urllib.error.HTTPError(url, 400, "Bad Request", {}, None)
            return Resp({"access_token": "tok", "expires_in": 5000, "token_type": "bearer"})
        body = req.data.decode()
        self.requests.append(("games", body))
        assert req.headers["Authorization"] == "Bearer tok" and req.headers["Client-id"] == "cid"
        if self.limit_once:
            self.limit_once = False
            raise urllib.error.HTTPError(url, 429, "Too Many Requests", {}, None)
        if "platforms = (" not in body:                                                    # (the key test's small query)
            return Resp([])
        pid = int(body.split("platforms = (")[1].split(")")[0])
        after = int(body.split("id > ")[1].split(";")[0])
        page = [g for g in self.games.get(pid, []) if g["id"] > after][:igdb.PAGE]
        return Resp(page)


def game(i: int, name: str, rating: float = 80.0, votes: int = 12, alts=()) -> dict:
    return {"id": i, "name": name, "total_rating": rating, "total_rating_count": votes, "first_release_date": 700000000,
            "alternative_names": [{"name": a} for a in alts]}


class IndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="romorg-igdb-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.fake = FakeIgdb({19: [game(1, "Super Mario World", 92.0, 300), game(2, "Zero Votes", 50.0, 0)],
                              18: [game(i, f"Game {i}", 60.0 + i % 30, 5 + i) for i in range(1, 1201)]})

    def build(self):
        return igdb.build_index(("cid", "s3cret"), self.dir, opener=self.fake, sleep=lambda s: None)

    def test_a_key_makes_an_index_of_the_rated_games_of_every_system(self) -> None:
        man = self.build()
        self.assertEqual((man["platforms"]["Super Nintendo Entertainment System"], man["platforms"]["Nintendo Entertainment System"]), (1, 1200))
        self.assertEqual(sorted(man["built_for"]), sorted(igdb.PLATFORM_IDS))
        pages = [r for k, r in self.fake.requests if k == "games" and "platforms = (18)" in r]
        self.assertEqual(len(pages), 3)                                                    # 500 + 500 + 200: paged by id
        self.assertIn("id > 500", pages[1])
        self.assertTrue(igdb.index_path(self.dir).is_file())
        self.assertEqual(igdb.installed(self.dir)["client"], man["client"])

    def test_its_ratings_are_on_the_scale_of_the_others_and_join_theirs(self) -> None:
        self.build()
        lb_rows = [("Super Nintendo Entertainment System", ratings.norm_title("Super Mario World"), 4.0, 50, 7, "Super Mario World", 1990, 0),
                   ("Super Nintendo Entertainment System", ratings.norm_title("F-Zero"), 4.5, 40, 8, "F-Zero", 1990, 0)]
        ratings.write_index(lb_rows, ratings.index_path(self.dir), {"schema": ratings.SCHEMA, "platforms": {}, "built_for": []})
        store = ratings.Store(self.dir)
        snes = platforms.get_platform("Super Nintendo Entertainment System")
        self.assertEqual(store.lookup(snes, "Super Mario World"), (9.2, 300))          # more votes than LaunchBox's 50: IGDB's counts
        self.assertEqual(store.lookup(snes, "F-Zero"), (9.0, 40))                      # only LaunchBox knows it
        self.assertIsNone(store.lookup(snes, "Zero Votes"))                            # (no votes: not in the index)
        os.remove(igdb.index_path(self.dir))
        self.assertEqual(ratings.Store(self.dir).lookup(snes, "Super Mario World"), (8.0, 50))   # without IGDB: LaunchBox's alone

    def test_an_index_of_an_older_build_does_not_answer_for_systems_it_was_made_without(self) -> None:
        rows = [("Super Nintendo Entertainment System", ratings.norm_title("F-Zero"), 4.5, 40, 8, "F-Zero", 1990, 0)]
        man = {"schema": ratings.SCHEMA, "platforms": {"Super Nintendo Entertainment System": 1}}          # (no built_for: an older build)
        ratings.write_index(rows, ratings.index_path(self.dir), man)
        (self.dir / "manifest.json").write_text(json.dumps(man))
        store = ratings.Store(self.dir)
        self.assertTrue(store.covers(platforms.get_platform("Super Nintendo Entertainment System")))
        self.assertFalse(store.covers(platforms.get_platform("Atari Lynx")))       # a rating filter must wait for a new index, not empty the system

    def test_a_wrong_secret_is_said_in_words(self) -> None:
        with self.assertRaises(igdb.IgdbError) as cm:
            igdb.build_index(("cid", "wrong"), self.dir, opener=self.fake, sleep=lambda s: None)
        self.assertIn("does not accept", str(cm.exception))
        self.assertFalse(igdb.index_path(self.dir).exists())

    def test_too_many_requests_waits_and_tries_again(self) -> None:
        self.fake.limit_once = True
        waited = []
        igdb.build_index(("cid", "s3cret"), self.dir, opener=self.fake, sleep=waited.append)
        self.assertTrue(any(w >= 1.0 for w in waited))

    def test_when_it_is_made_again(self) -> None:
        man = self.build()
        creds = ("cid", "s3cret")
        self.assertFalse(igdb.needs_refresh(man, creds, now=time.time()))
        self.assertTrue(igdb.needs_refresh(man, creds, now=time.time() + 8 * 86400))               # a week old
        self.assertTrue(igdb.needs_refresh(man, ("other", "s3cret")))                            # another key
        self.assertTrue(igdb.needs_refresh({**man, "built_for": ["Nintendo 64"]}, creds, now=time.time()))   # a system it was not made for
        self.assertTrue(igdb.needs_refresh(None, creds))
        self.assertFalse(igdb.needs_refresh(None, None))                                        # (no key: nothing to make)

    def test_the_key_is_tried_with_a_token_and_a_small_query(self) -> None:
        self.assertIn("works", igdb.check_credentials("cid", "s3cret", self.fake))
        with self.assertRaises(igdb.IgdbError):
            igdb.check_credentials("cid", "nope", self.fake)


class Endpoints(ServerCase):
    def test_the_key_is_kept_never_sent_back_and_taken_away_again(self) -> None:
        out = self.call("POST", "/api/ratings/igdb", {"client_id": "my-id", "client_secret": "my-secret"})
        igdb_block = out["ratings"]["igdb"]
        self.assertEqual((igdb_block["client_id"], igdb_block["has_secret"], igdb_block["configured"]), ("my-id", True, True))
        self.assertNotIn("my-secret", json.dumps(self.call("GET", "/api/ratings")))
        self.assertNotIn("my-secret", json.dumps(self.call("GET", "/api/status")))
        saved = json.loads((self.tmp / "data" / "config.json").read_text())
        self.assertEqual(saved["igdb"], {"client_id": "my-id", "client_secret": "my-secret"})
        again = self.call("POST", "/api/ratings/igdb", {"client_id": "my-id", "client_secret": ""})       # (only the id was shown: the secret stays)
        self.assertTrue(again["ratings"]["igdb"]["has_secret"])
        gone = self.call("POST", "/api/ratings/igdb", {"client_id": "", "client_secret": ""})
        self.assertEqual((gone["ratings"]["igdb"]["configured"], gone["ratings"]["igdb"]["has_secret"]), (False, False))

    def test_half_a_key_is_refused(self) -> None:
        code, _ = self.http("POST", "/api/ratings/igdb", {"client_id": "only-an-id", "client_secret": ""})
        self.assertEqual(code, 400)

    def test_the_test_button_says_whether_it_works(self) -> None:
        with mock.patch.object(igdb, "check_credentials", return_value="The key works."):
            self.assertEqual(self.call("POST", "/api/ratings/igdb/test", {"client_id": "a", "client_secret": "b"}), {"ok": True, "message": "The key works."})
        with mock.patch.object(igdb, "check_credentials", side_effect=igdb.IgdbError("Twitch does not accept that Client ID and secret")):
            out = self.call("POST", "/api/ratings/igdb/test", {"client_id": "a", "client_secret": "b"})
            self.assertFalse(out["ok"])
        code, _ = self.http("POST", "/api/ratings/igdb/test", {})
        self.assertEqual(code, 400)


if __name__ == "__main__":
    unittest.main()
