"""Game ratings (Amendment 18): the LaunchBox index / matching, the rating rules of ``library.select``, totals, the
updater orchestration and the server fields. Synthetic data only; no network."""

from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import os
import random
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import zipfile
import zlib
from dataclasses import replace
from email.utils import format_datetime
from pathlib import Path
from unittest import mock
os.environ.setdefault("ROMORG_RETROARCH_DETECT", "0")      # never pick up a RetroArch installed on this machine

from romorg import autoupdate, library, paths, platforms, ratings, server, tags, totals
from romorg.datfile import DatFile, Rom
from romorg.library import EXCLUDED, KEEP, Item, LibraryProfile, RatingContext
from romorg.tosec import ReleaseInfo

GBA = platforms.get_platform("Nintendo Game Boy Advance")
GBA_DAT = "Nintendo - Game Boy Advance"
AMIGA = platforms.get_platform("Commodore Amiga")
GAMES = "Commodore Amiga - Games - [ADF]"


# --------------------------------------------------------------------------- the synthetic Metadata.xml

def _game(dbid: int, name: str, platform: str, stars: float | None, votes: int | None, year: str = "1999") -> str:
    parts = [f"<Name>{name}</Name>", f"<ReleaseYear>{year}</ReleaseYear>", f"<DatabaseID>{dbid}</DatabaseID>"]
    if stars is not None:
        parts.append(f"<CommunityRating>{stars}</CommunityRating>")
    parts.append(f"<Platform>{platform}</Platform>")
    if votes is not None:
        parts.append(f"<CommunityRatingCount>{votes}</CommunityRatingCount>")
    parts.append("<Genres /><Developer>Dev &amp; Co</Developer>")
    return "  <Game>\n    " + "\n    ".join(parts) + "\n  </Game>\n"


def _alt(dbid: int, name: str, region: str = "") -> str:
    reg = f"<Region>{region}</Region>" if region else ""
    return f"  <GameAlternateName>\n    <AlternateName>{name}</AlternateName>\n    <DatabaseID>{dbid}</DatabaseID>\n    {reg}\n  </GameAlternateName>\n"


G = "Nintendo Game Boy Advance"
XML = ('<?xml version="1.0" standalone="yes"?>\n<LaunchBox>\n'
       + _game(1, "Alpha Quest", G, 4.5, 100) + _game(2, "Beta Blade", G, 3.5, 50)
       + _game(3, "Gamma Run", G, 0, 0) + _game(4, "Delta Force", G, 4.0, 3)
       + _game(5, "Epsilon", G, 3.0, 10) + _game(6, "Epsilon", G, 4.0, 40)               # duplicate name: most votes wins
       + _game(7, "The Legend of Zeta: Part II", G, 4.4, 60)
       + _game(8, "Eta Racing", G, 4.2, 20)
       + _game(9, "Alpha Quest", "Nintendo 64", 1.0, 10)                                   # other platform
       + _game(10, "Atari Thing", "Atari 2600", 5.0, 500)                                  # platform we do not know
       + _game(11, "Theta &amp; Iota", G, 4.0, 12)
       + _game(12, "Lambda Wonder Boy Land", G, 3.8, 30)
       + _game(13, "Pi Garden Worldz", G, 3.0, 9) + _game(14, "Pi Garden Wirlds", G, 3.1, 9)
       + _game(15, "Sigma Star Saga", G, 4.0, 70) + _game(16, "Mega Man 7", G, 4.5, 80)
       + _game(17, "Rho Run", G, None, None)                                               # no rating element at all
       + _game(18, "Tau Tactics", G, 3.0, 5)                                               # exactly 5 votes
       + _game(19, "Upsilon Hunt", G, 5.0, 1)                                              # 1 vote
       + _game(20, "Amiga Alpha", "Commodore Amiga", 4.0, 30)
       + _game(21, "Alpha", "Commodore Amiga", 4.5, 100) + _game(22, "Beta", "Commodore Amiga", 3.0, 50)
       + _game(23, "Delta", "Commodore Amiga", 4.0, 3) + _game(24, "Eps", "Commodore Amiga", 3.5, 20)
       + "  <Platform><Name>ignored</Name></Platform>\n  <GameImage><DatabaseID>1</DatabaseID></GameImage>\n"
       + _alt(8, "Racer Eta", "Europe") + _alt(8, "Eta GP") + _alt(3, "Gamma Alt") + _alt(999, "Nobody")
       + _alt(1, "Epsilon")                                                                 # an alt name equal to another game's name
       + "</LaunchBox>\n")


def make_zip(path: Path, xml: str = XML, extra: bool = True) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("Metadata.xml", xml)
        if extra:
            zf.writestr("Platforms.xml", "<LaunchBox/>")
            zf.writestr("Mame.xml", "<LaunchBox/>")
    return path


class TmpDirCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="romorg-ratings-test-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        env = mock.patch.dict(os.environ, {"ROMORG_DATA_DIR": str(self.tmp / "data"), "ROMORG_OFFLINE": "1"})
        env.start()
        self.addCleanup(env.stop)
        self.dir = self.tmp / "ratings"

    def build(self, xml: str = XML, meta: dict | None = None) -> dict:
        return ratings.build_index(make_zip(self.tmp / "Metadata.zip", xml), self.dir, meta=meta)


# --------------------------------------------------------------------------- normalisation

class NormTests(unittest.TestCase):
    def test_articles_subtitles_and_ampersand(self) -> None:
        n = ratings.norm_title
        self.assertEqual(n("Legend of Zelda, The - A Link to the Past"), n("The Legend of Zelda: A Link to the Past"))
        self.assertEqual(n("Legend of Zelda, The"), n("The Legend of Zelda"))
        self.assertEqual(n("Theta & Iota"), n("Theta and Iota"))
        self.assertEqual(n("Mario's Picross"), n("Marios Picross"))
        self.assertEqual(n("Pokémon Émeraude"), n("Pokemon Emeraude"))
        self.assertEqual(n("Mega Man - Battle Network 6"), n("Mega Man: Battle Network 6"))
        self.assertEqual(n("A Boy and His Blob"), n("Boy and His Blob, A"))
        self.assertEqual(n("  !!! "), "")
        self.assertNotEqual(n("Alpha 2"), n("Alpha 3"))

    def test_roman_and_numbers(self) -> None:
        self.assertEqual(ratings.roman_key("final fantasy ii"), "final fantasy 2")
        self.assertEqual(ratings.roman_key("rocky i"), "rocky i")             # a lone I is not read as a number
        self.assertEqual(ratings.number_signature("street fighter v"), (5,))
        self.assertEqual(ratings.number_signature("street fighter ii"), (2,))
        self.assertEqual(ratings.number_signature("v for vendetta"), ())      # a leading V is a word
        self.assertEqual(ratings.number_signature("game 2 1999"), (2, 1999))


# --------------------------------------------------------------------------- the streaming parser / index

class IndexTests(TmpDirCase):
    def test_parse_keeps_rated_games_of_known_platforms_only(self) -> None:
        games, alts = ratings.parse_metadata(io.BytesIO(XML.encode()), set(ratings.LB_PLATFORMS.values()))
        names = {g["name"] for g in games.values()}
        self.assertIn("Alpha Quest", names)
        self.assertNotIn("Gamma Run", names)            # rating 0 = unrated
        self.assertNotIn("Rho Run", names)              # no rating element
        self.assertNotIn("Atari Thing", names)          # other platform
        self.assertEqual(games[11]["name"], "Theta & Iota")       # XML entities decoded
        self.assertEqual(games[1]["year"], 1999)
        self.assertEqual(alts.get(8), ["Racer Eta", "Eta GP"])
        self.assertNotIn(999, alts)                     # alternate name of an unknown / unrated game
        self.assertNotIn(3, alts)                       # alt names of an unrated game are dropped too

    def test_build_index_manifest_and_files(self) -> None:
        man = self.build(meta={"etag": '"x"', "last_modified": "Sun, 04 Oct 2026 08:00:43 GMT"})
        self.assertEqual(man["schema"], ratings.SCHEMA)
        self.assertEqual(set(man["platforms"]), {G, "Nintendo 64", "Commodore Amiga"})
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["manifest.json", "ratings.sqlite"])   # no .part left
        self.assertEqual(ratings.installed(self.dir)["etag"], '"x"')
        self.assertEqual(ratings.source_date(ratings.installed(self.dir)), "2026-10-04")
        self.assertLess(ratings.age_days(ratings.installed(self.dir)), 0.01)

    def test_duplicates_most_votes_and_own_name_beats_alt(self) -> None:
        self.build()
        st = ratings.Store(self.dir)
        d = st.detail(G, "Epsilon")
        self.assertEqual((d["dbid"], d["rating"], d["votes"], d["kind"]), (6, 8.0, 40, "exact"))
        # the alt name "Epsilon" of game 1 loses against the game that IS called Epsilon
        self.assertEqual(st.detail(G, "Epsilon")["dbid"], 6)

    def test_scale_is_stars_times_two_with_one_decimal(self) -> None:
        self.build()
        st = ratings.Store(self.dir)
        self.assertEqual(st.lookup(G, "Alpha Quest"), (9.0, 100))
        self.assertEqual(st.lookup(G, "Lambda Wonder Boy Land"), (7.6, 30))
        self.assertEqual(ratings.format_rating(8.4, 123), "8.4 · 123 votes")
        self.assertEqual(ratings.format_rating(5.0, 1), "5.0 · 1 vote")

    def test_platforms_are_separate(self) -> None:
        self.build()
        st = ratings.Store(self.dir)
        self.assertEqual(st.lookup("Nintendo 64", "Alpha Quest"), (2.0, 10))
        self.assertEqual(st.lookup(G, "Alpha Quest"), (9.0, 100))
        self.assertIsNone(st.lookup("Sega Dreamcast", "Alpha Quest"))
        self.assertIsNone(st.lookup("No Such System", "Alpha Quest"))

    def test_bad_input_never_replaces_a_good_index(self) -> None:
        self.build()
        before = (self.dir / "ratings.sqlite").read_bytes()
        broken = make_zip(self.tmp / "bad.zip", "<LaunchBox><Game><Name>x</Name>")
        with self.assertRaises(ratings.RatingsError):
            ratings.build_index(broken, self.dir)
        notzip = self.tmp / "nozip.zip"
        notzip.write_bytes(b"not a zip")
        with self.assertRaises(ratings.RatingsError):
            ratings.build_index(notzip, self.dir)
        empty = make_zip(self.tmp / "empty.zip", "<LaunchBox></LaunchBox>")
        with self.assertRaises(ratings.RatingsError):
            ratings.build_index(empty, self.dir)
        nometa = self.tmp / "nometa.zip"
        with zipfile.ZipFile(nometa, "w") as zf:
            zf.writestr("Other.xml", "<LaunchBox/>")
        with self.assertRaises(ratings.RatingsError):
            ratings.build_index(nometa, self.dir)
        self.assertEqual((self.dir / "ratings.sqlite").read_bytes(), before)
        self.assertFalse(any(p.name.endswith(".part") for p in self.dir.iterdir()))

    def test_store_reloads_when_the_index_is_replaced(self) -> None:
        st = ratings.Store(self.dir)
        st.check_interval = 0
        self.assertFalse(st.available())
        self.assertIsNone(st.lookup(G, "Alpha Quest"))
        self.build()
        self.assertTrue(st.available())
        self.assertEqual(st.lookup(G, "Alpha Quest"), (9.0, 100))
        first = ratings.index_path(self.dir).stat()
        self.build(XML.replace("<CommunityRating>4.5</CommunityRating>", "<CommunityRating>2.0</CommunityRating>", 1))
        idx = ratings.index_path(self.dir)
        os.utime(idx, ns=(first.st_mtime_ns, first.st_mtime_ns))       # same size, same mtime tick (coarse clocks, tmpfs)
        self.assertEqual(idx.stat().st_size, first.st_size)
        self.assertEqual(st.lookup(G, "Alpha Quest"), (4.0, 100))      # new file -> caches dropped

    def test_default_store_follows_the_data_dir(self) -> None:
        self.assertIsNone(ratings.lookup(G, "Alpha Quest"))
        ratings.build_index(make_zip(self.tmp / "m.zip"))              # into paths.ratings_dir()
        self.assertEqual(ratings.lookup(G, "Alpha Quest"), (9.0, 100))
        self.assertEqual(paths.ratings_dir(), self.tmp / "data" / "ratings")


# --------------------------------------------------------------------------- matching

class MatchTests(TmpDirCase):
    def setUp(self) -> None:
        super().setUp()
        self.build()
        self.st = ratings.Store(self.dir)

    def kind(self, title: str) -> str | None:
        d = self.st.detail(G, title)
        return d["kind"] if d else None

    def test_exact_with_article_subtitle_and_ampersand(self) -> None:
        self.assertEqual(self.kind("Alpha Quest"), "exact")
        self.assertEqual(self.kind("alpha quest"), "exact")
        self.assertEqual(self.kind("Theta and Iota"), "exact")
        self.assertEqual(self.kind("Theta & Iota"), "exact")
        self.assertEqual(self.kind("Legend of Zeta, The - Part II"), "exact")

    def test_roman_numerals_only_when_unique(self) -> None:
        d = self.st.detail(G, "Legend of Zeta, The - Part 2")
        self.assertEqual((d["dbid"], d["kind"]), (7, "roman"))
        self.assertIsNone(self.st.detail(G, "Legend of Zeta, The - Part 3"))

    def test_alternate_names(self) -> None:
        for title in ("Racer Eta", "Eta GP"):
            d = self.st.detail(G, title)
            self.assertEqual((d["dbid"], d["kind"], d["name"]), (8, "alt", "Eta Racing"))
        self.assertEqual(self.kind("Eta Racing"), "exact")

    def test_fuzzy_is_accepted_when_unique_and_close(self) -> None:
        d = self.st.detail(G, "Lambda Wonderboy Land")
        self.assertEqual((d["dbid"], d["kind"]), (12, "fuzzy"))
        self.assertGreaterEqual(d["score"], ratings.FUZZY_MIN)

    def test_fuzzy_refuses_when_two_games_are_about_equally_close(self) -> None:
        self.assertIsNone(self.st.detail(G, "Pi Garden Worlds"))        # Worldz and Wirlds are equally near

    def test_fuzzy_refuses_other_numbers_and_added_words(self) -> None:
        self.assertIsNone(self.st.detail(G, "Mega Man 6"))              # digits differ: another game
        self.assertIsNone(self.st.detail(G, "Mega Man VI"))             # roman digits too
        self.assertIsNone(self.st.detail(G, "Sigma Star Saga DX"))      # only adds a word
        self.assertIsNone(self.st.detail(G, "Alpha Quests"))            # singular / plural only
        self.assertIsNone(self.st.detail(G, "Zzz Unknown Title"))
        self.assertIsNone(self.st.detail(G, ""))

    def test_a_missing_rating_beats_a_wrong_one_for_unrated_games(self) -> None:
        self.assertIsNone(self.st.detail(G, "Gamma Run"))               # unrated in LaunchBox: not indexed
        self.assertIsNone(self.st.detail(G, "Rho Run"))

    def test_lookup_is_cached_and_deterministic(self) -> None:
        a = self.st.detail(G, "Lambda Wonderboy Land")
        with mock.patch.object(ratings._Plat, "fuzzy", side_effect=AssertionError("cached")):
            self.assertIs(self.st.detail(G, "Lambda Wonderboy Land"), a)


# --------------------------------------------------------------------------- check / download

class FakeResp:
    def __init__(self, body: bytes = b"", headers: dict | None = None, status: int = 200) -> None:
        self.status, self.headers, self._io = status, headers or {}, io.BytesIO(body)

    def read(self, n: int = -1) -> bytes:
        return self._io.read(n)

    def close(self) -> None:
        pass


def http_date(days_ago: float) -> str:
    return format_datetime(dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days_ago), usegmt=True)


class DownloadTests(TmpDirCase):
    def setUp(self) -> None:
        super().setUp()
        self.zipbytes = make_zip(self.tmp / "src.zip").read_bytes()
        self.calls: list[str] = []
        self.remote_lm = http_date(0.5)

    def opener(self, req: urllib.request.Request):
        self.calls.append(req.get_method())
        headers = {"Last-Modified": self.remote_lm, "ETag": '"e1"', "Content-Length": str(len(self.zipbytes))}
        return FakeResp(self.zipbytes if req.get_method() == "GET" else b"", headers)

    def age(self, days: float) -> None:
        man = json.loads((self.dir / "manifest.json").read_text())
        man["built_at"] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat(timespec="seconds")
        man["last_modified"] = http_date(days + 0.2)
        (self.dir / "manifest.json").write_text(json.dumps(man))

    def test_download_builds_the_index_and_discards_the_zip(self) -> None:
        seen = []
        man = ratings.download_and_build(self.dir, progress=lambda d, t, m: seen.append(m), opener=self.opener)
        self.assertEqual(man["etag"], '"e1"')
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["manifest.json", "ratings.sqlite"])
        self.assertTrue(any("Downloading" in m for m in seen) and any("index" in m.lower() for m in seen))
        self.assertEqual(ratings.Store(self.dir).lookup(G, "Alpha Quest"), (9.0, 100))

    def test_missing_index_is_a_download_regardless_of_age(self) -> None:
        row = ratings.check_update(self.opener, self.dir)
        self.assertEqual(row["status"], "missing")
        self.assertEqual(self.calls, ["HEAD"])

    def test_seven_day_gate(self) -> None:
        ratings.download_and_build(self.dir, opener=self.opener)
        self.calls.clear()
        self.age(2)                                          # younger than 7 days: not even asked
        row = ratings.check_update(self.opener, self.dir)
        self.assertEqual((row["status"], self.calls), ("up_to_date", []))
        self.remote_lm = http_date(0.1)                      # newer data exists, index still young: still nothing
        self.assertEqual(ratings.check_update(self.opener, self.dir)["status"], "up_to_date")
        self.assertEqual(self.calls, [])
        self.assertEqual(ratings.check_update(self.opener, self.dir, gate=False)["status"], "update_available")  # explicit ask
        self.age(9)                                          # older than 7 days and the server has newer data
        self.calls.clear()
        self.remote_lm = http_date(0.1)
        row = ratings.check_update(self.opener, self.dir)
        self.assertEqual((row["status"], self.calls), ("update_available", ["HEAD"]))
        self.remote_lm = http_date(20)                       # old index but the server has nothing newer
        self.assertEqual(ratings.check_update(self.opener, self.dir)["status"], "up_to_date")

    def test_offline_and_http_errors_never_raise_and_keep_the_cache(self) -> None:
        ratings.download_and_build(self.dir, opener=self.opener)
        self.age(30)

        def offline(req):
            raise urllib.error.URLError("no route")

        row = ratings.check_update(offline, self.dir)
        self.assertEqual(row["status"], "error")
        self.assertIsNotNone(ratings.installed(self.dir))
        with self.assertRaises(ratings.RatingsError):
            ratings.download_and_build(self.dir, opener=offline)
        self.assertEqual(ratings.Store(self.dir).lookup(G, "Alpha Quest"), (9.0, 100))     # untouched

        def http500(req):
            return FakeResp(b"", {}, 500)

        self.assertEqual(ratings.check_update(http500, self.dir)["status"], "error")

    def test_incomplete_or_corrupt_download_leaves_the_old_index(self) -> None:
        ratings.download_and_build(self.dir, opener=self.opener)
        before = (self.dir / "ratings.sqlite").read_bytes()

        def short(req):
            return FakeResp(self.zipbytes[:100], {"Content-Length": str(len(self.zipbytes))})

        with self.assertRaises(ratings.RatingsError):
            ratings.download_and_build(self.dir, opener=short)

        def garbage(req):
            return FakeResp(b"x" * 500, {"Content-Length": "500"})

        with self.assertRaises(ratings.RatingsError):
            ratings.download_and_build(self.dir, opener=garbage)
        self.assertEqual((self.dir / "ratings.sqlite").read_bytes(), before)
        self.assertFalse(any(p.name.endswith(".part") for p in self.dir.iterdir()))

    def test_cancel(self) -> None:
        ev = threading.Event()
        ev.set()
        from romorg.tosec import Cancelled
        with self.assertRaises(Cancelled):
            ratings.download_and_build(self.dir, cancel=ev, opener=self.opener)
        self.assertFalse(any(self.dir.iterdir()) if self.dir.exists() else False)


# --------------------------------------------------------------------------- the rules

def gba_items(names: list[str]) -> list[Item]:
    return [Item(key=i, dat=GBA_DAT, rom=Rom(name=n + ".gba", size=1, crc="", md5="", sha1="", game=n, dat=GBA_DAT,
                                              set_name=n), style="nointro", path=Path(n), member=None)
            for i, n in enumerate(names, 1)]


def amiga_items(names: list[str]) -> list[Item]:
    return [Item(key=i, dat=GAMES, rom=Rom(name=n + ".adf", size=1, crc="", md5="", sha1="", game=n, dat=GAMES),
                 style="tosec", path=Path(n), member=None) for i, n in enumerate(names, 1)]


def ctx_of(table: dict[str, tuple[float, int]], cutoff=None) -> RatingContext:
    return RatingContext(lambda title: table.get(title), cutoff)


GBA_TABLE = {"Ace": (9.0, 100), "Bolt": (8.0, 50), "Cube": (8.0, 80), "Dune": (7.0, 40), "Echo": (6.0, 200),
             "Flux": (9.5, 4)}                      # Flux: 4 votes = unrated; Gust has no rating at all
GBA_NAMES = [f"{t} (USA)" for t in ("Ace", "Bolt", "Cube", "Dune", "Echo", "Flux", "Gust")]
FILTER = replace(library.default_profile(GBA), min_votes=5)


def decide(names, profile, table=GBA_TABLE, cutoff=None, items_fn=gba_items, platform=GBA):
    items = items_fn(names)
    sel = library.select(items, profile, platform, ratings=ctx_of(table, cutoff))
    return items, sel, {names[k - 1]: d for k, d in sel.decisions.items()}


def kept(by: dict) -> set[str]:
    return {n.split(" (")[0] for n, d in by.items() if d.action == KEEP}


class ProfileFieldTests(unittest.TestCase):
    def test_defaults_are_off_and_old_profiles_load(self) -> None:
        p = library.default_profile(GBA)
        self.assertEqual((p.min_rating, p.top_n, p.min_votes, p.keep_unrated, p.rank_scope, p.rating_active),
                         (None, None, 5, False, "dat", False))
        old = {"exclude": ["demo"], "latest_only": True}
        q = LibraryProfile.from_dict(old, p)
        self.assertFalse(q.rating_active)
        self.assertEqual((q.min_votes, q.rank_scope), (5, "dat"))
        self.assertFalse(library.load_profile({"library": {GBA.name: old}}, GBA).rating_active)

    def test_roundtrip_and_normalisation(self) -> None:
        p = replace(library.default_profile(GBA), min_rating=7.5, top_n=300, min_votes=10, keep_unrated=True,
                    rank_scope="owned")
        d = json.loads(json.dumps(p.to_dict()))
        self.assertEqual(LibraryProfile.from_dict(d, library.default_profile(GBA)), p)
        q = LibraryProfile(min_rating=0, top_n=0, min_votes=0, rank_scope="bogus")
        self.assertEqual((q.min_rating, q.top_n, q.min_votes, q.rank_scope), (None, None, 5, "dat"))
        q = LibraryProfile.from_dict({"min_rating": "x", "top_n": True, "min_votes": -3, "rank_scope": 7,
                                      "keep_unrated": "yes"}, library.default_profile(GBA))
        self.assertEqual((q.min_rating, q.top_n, q.min_votes, q.rank_scope, q.keep_unrated), (None, None, 5, "dat", False))
        self.assertEqual(LibraryProfile(min_rating=11).min_rating, None)
        self.assertEqual(LibraryProfile(min_rating=7.26).min_rating, 7.3)

    def test_changing_a_rating_option_changes_the_totals_signature(self) -> None:
        a = library.default_profile(GBA)
        sigs = {totals.profile_signature(replace(a, **kw)) for kw in
                ({}, {"min_rating": 7.0}, {"top_n": 10}, {"min_votes": 9}, {"keep_unrated": True}, {"rank_scope": "owned"})}
        self.assertEqual(len(sigs), 6)

    def test_catalog_group_and_availability(self) -> None:
        cat = library.rule_catalog("nointro")
        grp = [e for e in cat if e.get("group") == "ratings"]
        self.assertEqual([e["id"] for e in grp], ["min_rating", "top_n", "min_votes", "keep_unrated", "rank_scope"])
        self.assertEqual({e["id"]: e["kind"] for e in grp}["rank_scope"], "choice")
        self.assertEqual([c["value"] for c in grp[-1]["choices"]], ["dat", "owned"])
        for style in ("tosec", "nointro", "whdload", "redump"):
            self.assertEqual(len([e for e in library.rule_catalog(style) if e.get("group") == "ratings"]), 5)
        info = library.profile_info(GBA)
        self.assertTrue(info["available"]["ratings"])
        self.assertEqual(info["rating_codes"], ["rating_low", "rating_not_top", "rating_unrated"])
        self.assertTrue(all(c in info["reason_labels"] for c in info["rating_codes"]))
        self.assertTrue(set(library.RATING_CODES) <= set(library.ALL_CODES))
        fake = mock.Mock(spec=["name", "dats"], dats=("x",))
        fake.name = "Unknown Machine"
        self.assertFalse(library.profile_info(fake)["available"]["ratings"])
        self.assertEqual(ratings.rated_dats(fake), ())
        self.assertEqual(ratings.rated_dats(AMIGA), (GAMES,))


class RuleTests(unittest.TestCase):
    def test_no_filter_changes_nothing_and_needs_no_data(self) -> None:
        items = gba_items(GBA_NAMES)
        plain = library.select(items, library.default_profile(GBA), GBA)
        with_ctx = library.select(items, library.default_profile(GBA), GBA, ratings=ctx_of(GBA_TABLE))
        self.assertEqual({k: d.action for k, d in plain.decisions.items()}, {k: d.action for k, d in with_ctx.decisions.items()})
        self.assertEqual(plain.rating, {})
        self.assertTrue(all(d.action == KEEP for d in plain.decisions.values()))

    def test_a_filter_without_data_is_an_error_never_a_silent_pass(self) -> None:
        with self.assertRaises(library.RatingsUnavailable):
            library.select(gba_items(GBA_NAMES), replace(FILTER, min_rating=7.0), GBA)
        with self.assertRaises(library.RatingsUnavailable):
            library.select(gba_items(GBA_NAMES), replace(FILTER, top_n=3), GBA, ratings=ctx_of(GBA_TABLE))   # dat scope needs the cutoff

    def test_min_rating_excludes_rated_below_and_unrated(self) -> None:
        _items, sel, by = decide(GBA_NAMES, replace(FILTER, min_rating=7.0))
        self.assertEqual(kept(by), {"Ace", "Bolt", "Cube", "Dune"})
        self.assertEqual(by["Echo (USA)"].codes, ("rating_low",))
        self.assertEqual(by["Flux (USA)"].codes, ("rating_unrated",))          # 4 votes < min_votes
        self.assertEqual(by["Gust (USA)"].codes, ("rating_unrated",))
        self.assertIn("6.0", by["Echo (USA)"].reason)
        self.assertEqual(sel.rating["excluded_by"], {"rating_low": 1, "rating_unrated": 2})
        self.assertEqual((sel.rating["games"], sel.rating["rated"], sel.rating["kept"]), (7, 5, 4))

    def test_threshold_is_inclusive(self) -> None:
        self.assertIn("Dune", kept(decide(GBA_NAMES, replace(FILTER, min_rating=7.0))[2]))
        self.assertNotIn("Dune", kept(decide(GBA_NAMES, replace(FILTER, min_rating=7.1))[2]))

    def test_min_votes_decides_what_counts_as_rated(self) -> None:
        by = decide(GBA_NAMES, replace(FILTER, min_rating=7.0, min_votes=3))[2]
        self.assertIn("Flux", kept(by))                                        # 4 votes now enough, 9.5 >= 7
        by = decide(GBA_NAMES, replace(FILTER, min_rating=7.0, min_votes=60))[2]
        self.assertEqual(kept(by), {"Ace", "Cube", "Echo"} - {"Echo"})         # only >= 60 votes count; Echo rated 6.0 < 7

    def test_keep_unrated_keeps_them_in_addition(self) -> None:
        by = decide(GBA_NAMES, replace(FILTER, min_rating=7.0, keep_unrated=True))[2]
        self.assertEqual(kept(by), {"Ace", "Bolt", "Cube", "Dune", "Flux", "Gust"})
        self.assertEqual(by["Echo (USA)"].action, EXCLUDED)

    def test_top_n_dat_scope_uses_the_target_cutoff_and_ties_break_by_votes(self) -> None:
        items = gba_items(GBA_NAMES)
        prof = replace(FILTER, top_n=3)
        cut = library.target_cutoff(items, prof, GBA, GBA_TABLE.get)
        self.assertEqual(cut[:3], (-8.0, -50, "bolt"))                          # 3rd best: Ace 9.0, Cube 8.0/80, Bolt 8.0/50
        by = decide(GBA_NAMES, prof, cutoff=cut)[2]
        self.assertEqual(kept(by), {"Ace", "Cube", "Bolt"})
        self.assertEqual(by["Dune (USA)"].codes, ("rating_not_top",))
        self.assertEqual(by["Gust (USA)"].codes, ("rating_unrated",))
        by = decide(GBA_NAMES, replace(prof, keep_unrated=True), cutoff=cut)[2]
        self.assertEqual(kept(by), {"Ace", "Cube", "Bolt", "Flux", "Gust"})     # unrated in addition to the top N

    def test_top_n_larger_than_the_pool_keeps_every_rated_game(self) -> None:
        items = gba_items(GBA_NAMES)
        prof = replace(FILTER, top_n=50)
        cut = library.target_cutoff(items, prof, GBA, GBA_TABLE.get)
        self.assertEqual(cut, library.INF_KEY)
        self.assertEqual(kept(decide(GBA_NAMES, prof, cutoff=cut)[2]), {"Ace", "Bolt", "Cube", "Dune", "Echo"})

    def test_dat_scope_never_evicts_when_files_are_added(self) -> None:
        prof = replace(FILTER, top_n=3)
        cut = library.target_cutoff(gba_items(GBA_NAMES), prof, GBA, GBA_TABLE.get)
        small = kept(decide(GBA_NAMES[:3], prof, cutoff=cut)[2])               # Ace, Bolt, Cube (the user owns three)
        more = kept(decide(GBA_NAMES, prof, cutoff=cut)[2])
        self.assertEqual(small, {"Ace", "Bolt", "Cube"})
        self.assertTrue(small <= more)

    def test_owned_scope_ranks_only_among_the_owned_games(self) -> None:
        prof = replace(FILTER, top_n=2, rank_scope="owned")
        by = decide(["Dune (USA)", "Echo (USA)", "Bolt (USA)"], prof)[2]
        self.assertEqual(kept(by), {"Bolt", "Dune"})                            # the best two OF WHAT IS OWNED
        by = decide(GBA_NAMES, prof)[2]
        self.assertEqual(kept(by), {"Ace", "Cube"})

    def test_min_rating_and_top_n_together(self) -> None:
        items = gba_items(GBA_NAMES)
        prof = replace(FILTER, min_rating=7.5, top_n=10)
        cut = library.target_cutoff(items, prof, GBA, GBA_TABLE.get)
        by = decide(GBA_NAMES, prof, cutoff=cut)[2]
        self.assertEqual(kept(by), {"Ace", "Bolt", "Cube"})
        self.assertEqual(by["Dune (USA)"].codes, ("rating_low",))

    def test_games_of_one_title_stay_or_go_together_at_the_cutoff(self) -> None:
        names = ["Twin (USA)", "Twin (USA) (Unl)", "Solo (USA)"]
        table = {"Twin": (8.0, 10), "Solo": (8.0, 9)}
        prof = replace(FILTER, top_n=1, rank_scope="owned")
        by = decide(names, prof, table)[2]
        self.assertEqual(kept(by), {"Twin"})
        self.assertEqual({n for n, d in by.items() if d.action == KEEP}, {"Twin (USA)", "Twin (USA) (Unl)"})

    def test_all_variants_of_a_game_share_its_rating_and_the_other_rules_come_first(self) -> None:
        names = ["Ace (USA)", "Ace (Europe)", "Ace (USA) (Beta)", "Ace (Japan) (Ja)", "Echo (USA)"]
        by = decide(names, replace(FILTER, min_rating=7.0))[2]
        self.assertEqual(by["Ace (Europe)"].action, KEEP)                       # one version per game (Europe first) keeps one
        self.assertEqual(by["Ace (USA)"].action, "superseded")                  # decided by the other rules, not rating
        self.assertEqual(by["Ace (USA) (Beta)"].codes, ("pre_release",))
        self.assertEqual(by["Ace (Japan) (Ja)"].codes, ("language",))
        self.assertEqual(by["Echo (USA)"].codes, ("rating_low",))

    def test_multi_disk_game_is_one_game_with_one_rating(self) -> None:
        names = ["Quest (1990)(Pub)(Disk 1 of 2)", "Quest (1990)(Pub)(Disk 2 of 2)", "Dull (1990)(Pub)(Disk 1 of 2)",
                 "Dull (1990)(Pub)(Disk 2 of 2)", "Free (1990)(Pub)"]
        table = {"Quest": (9.0, 50), "Dull": (3.0, 50)}
        prof = replace(library.default_profile(AMIGA), min_rating=7.0, keep_unrated=True)
        items, sel, by = decide(names, prof, table, items_fn=amiga_items, platform=AMIGA)
        self.assertEqual({n for n, d in by.items() if d.action == KEEP},
                         {names[0], names[1], names[4]})
        self.assertEqual(by[names[2]].codes, by[names[3]].codes)
        self.assertEqual(by[names[2]].codes, ("rating_low",))
        self.assertEqual([cs.name for cs in sel.sets], ["Quest (1990)(Pub)"])   # Dull's playlist is gone

    def test_dats_without_games_are_not_rated(self) -> None:
        wb = "Commodore Amiga - Operating Systems - Workbench"
        items = [Item(key=1, dat=wb, rom=Rom(name="Workbench 3.1.adf", size=1, crc="", md5="", sha1="", game="W", dat=wb),
                      style="tosec", path=Path("w"), member=None)]
        sel = library.select(items, replace(library.default_profile(AMIGA), min_rating=9.0), AMIGA,
                             ratings=ctx_of({}))
        self.assertEqual(sel.decisions[1].action, KEEP)

    def test_vanish_report_hints(self) -> None:
        _items, sel, _by = decide(GBA_NAMES, replace(FILTER, min_rating=7.0))
        rep = library.vanish_report(sel, None)
        by = {i["title"]: i for i in rep["items"]}
        self.assertEqual(by["Echo"]["reason"], "rating_low")
        self.assertIn("lower the minimum rating", by["Echo"]["hint"])
        self.assertIn("rated 6.0 (200 votes)", by["Echo"]["text"])
        self.assertEqual(by["Gust"]["reason"], "rating_unrated")
        self.assertIn("Keep unrated games", by["Gust"]["hint"])
        self.assertEqual(rep["by_reason"], {"rating_low": 1, "rating_unrated": 2})
        _i, sel2, _b = decide(GBA_NAMES, replace(FILTER, top_n=3), cutoff=(-8.0, -50, "bolt", ""))
        hint = {i["title"]: i["hint"] for i in library.vanish_report(sel2, None)["items"]}
        self.assertIn("raise Top N", hint["Dune"])
        self.assertEqual(len(library.vanish_report(sel2, None, "rating_not_top")["items"]), 2)
        self.assertTrue(all(i["reason"] == "rating_not_top" for i in library.vanish_report(sel2, None, "rating_not_top")["items"]))

    def test_summary_counts_in_the_selection(self) -> None:
        _i, sel, _b = decide(GBA_NAMES, replace(FILTER, min_rating=9.0))
        self.assertEqual(sel.rating["excluded"], 6)
        self.assertEqual(sel.exclusion_counts()["exclusive"]["rating_low"], 4)
        self.assertEqual(sel.exclusion_counts()["exclusive"]["rating_unrated"], 2)


class IdempotenceProperty(unittest.TestCase):
    """Re-selecting the kept output changes nothing - both rank scopes, random profiles, shuffled input."""

    NAMES = [f"{t} ({r})" for t in ("Ace", "Bolt", "Cube", "Dune", "Echo", "Flux", "Gust", "Hex", "Ion", "Jet")
             for r in ("USA", "Europe")] + ["Ace (USA) (Rev 1)", "Bolt (Japan) (Ja)", "Cube (USA) (Beta)", "Dune (USA) (Unl)"]

    def check(self, seed: int) -> None:
        rng = random.Random(seed)
        table = {t: (round(rng.uniform(1, 10), 1), rng.choice([2, 5, 5, 9, 40, 300]))
                 for t in ("Ace", "Bolt", "Cube", "Dune", "Echo", "Hex", "Ion") if rng.random() < 0.85}
        prof = replace(library.default_profile(GBA),
                       min_rating=rng.choice([None, 3.0, 5.5, 7.0, 9.0]), top_n=rng.choice([None, 1, 2, 4, 20]),
                       min_votes=rng.choice([1, 5, 10]), keep_unrated=rng.random() < 0.5,
                       rank_scope=rng.choice(["dat", "owned"]),
                       languages=rng.choice([("En",), (), ("En", "Ja")]), one_per_game=rng.random() < 0.7,
                       latest_only=rng.random() < 0.8)
        all_items = gba_items(self.NAMES)
        cut = library.target_cutoff(all_items, prof, GBA, table.get) if prof.rating_active else None
        owned = [it for it in all_items if rng.random() < 0.7]
        owned = [replace(it, key=i) for i, it in enumerate(owned, 1)]
        ctx = RatingContext(table.get, cut)
        sel = library.select(owned, prof, GBA, ratings=ctx)
        keep = [it for it in owned if sel.decisions[it.key].action == KEEP]
        rng.shuffle(keep)
        keep = [replace(it, key=i) for i, it in enumerate(keep, 1)]
        again = library.select(keep, prof, GBA, ratings=ctx)
        self.assertEqual([it.rom.name for it in keep if again.decisions[it.key].action != KEEP], [], (seed, prof))
        shuffled = owned[:]
        rng.shuffle(shuffled)
        sel3 = library.select(shuffled, prof, GBA, ratings=ctx)
        by_name = lambda items, s: {it.rom.name: s.decisions[it.key].action for it in items}      # noqa: E731
        self.assertEqual(by_name(owned, sel), by_name(shuffled, sel3), (seed, prof))

    def test_random_profiles(self) -> None:
        for seed in range(150):
            self.check(seed)

    def test_amiga_multi_disk(self) -> None:
        names = ["Q (1990)(P)(Disk 1 of 2)", "Q (1990)(P)(Disk 2 of 2)", "Q (1990)(P)(DE)(Disk 1 of 2)", "Q (1990)(P)(DE)(Disk 2 of 2)",
                 "R (1990)(P)(Disk 1 of 2)", "R (1990)(P)(Disk 2 of 2)", "S (1990)(P)", "S (1990)(P)(AGA)", "T (1991)(P)[cr X]"]
        for seed in range(60):
            rng = random.Random(seed)
            table = {"Q": (rng.uniform(1, 10), 20), "R": (rng.uniform(1, 10), 30), "S": (rng.uniform(1, 10), 10)}
            prof = replace(library.default_profile(AMIGA), min_rating=rng.choice([None, 4.0, 7.0]),
                           top_n=rng.choice([None, 1, 2]), rank_scope=rng.choice(["dat", "owned"]),
                           keep_unrated=rng.random() < 0.5, languages=rng.choice([("En",), ("En", "De")]))
            items = amiga_items(names)
            cut = library.target_cutoff(items, prof, AMIGA, table.get) if prof.rating_active else None
            ctx = RatingContext(table.get, cut)
            sel = library.select(items, prof, AMIGA, ratings=ctx)
            keep = [replace(it, key=i) for i, it in enumerate([it for it in items if sel.decisions[it.key].action == KEEP], 1)]
            again = library.select(keep, prof, AMIGA, ratings=ctx)
            self.assertEqual([it.rom.name for it in keep if again.decisions[it.key].action != KEEP], [], (seed, prof))


# --------------------------------------------------------------------------- totals

def dat_of(names: list[str]) -> DatFile:
    return DatFile(name=GBA_DAT, description=GBA_DAT, version="1",
                   roms=[Rom(name=n + ".gba", size=1, crc="", md5="", sha1="", game=n, dat=GBA_DAT, set_name=n) for n in names])


class TotalsRatingTests(unittest.TestCase):
    lookup = staticmethod(GBA_TABLE.get)

    def test_target_counts_games_after_all_rules_including_rating(self) -> None:
        dat = dat_of(GBA_NAMES + ["Ace (USA) (Beta)"])
        base = totals.compute_target(GBA, [dat], FILTER)
        self.assertEqual(base.count, 7)
        t = totals.compute_target(GBA, [dat], replace(FILTER, min_rating=7.0), self.lookup)
        self.assertEqual(t.count, 4)
        t2 = totals.compute_target(GBA, [dat], replace(FILTER, min_rating=7.0, keep_unrated=True), self.lookup)
        self.assertEqual(t2.count, 6)
        t3 = totals.compute_target(GBA, [dat], replace(FILTER, top_n=3), self.lookup)
        self.assertEqual(t3.count, 3)
        self.assertEqual(t3.cutoff[:3], (-8.0, -50, "bolt"))
        self.assertEqual((t3.rating["games"], t3.rating["rated"]), (7, 5))
        self.assertEqual(t3.coverage["games"], 7)
        self.assertEqual(t3.coverage["rated"], 5)

    def test_coverage_and_histogram_without_a_filter(self) -> None:
        t = totals.compute_target(GBA, [dat_of(GBA_NAMES)], FILTER, self.lookup)
        self.assertEqual((t.count, t.coverage["games"], t.coverage["rated"]), (7, 7, 5))
        self.assertEqual(len(t.coverage["ge"]), 20)
        self.assertEqual(t.coverage["ge"][13], 4)               # >= 7.0: Ace, Bolt, Cube, Dune
        self.assertEqual(t.coverage["ge"][17], 1)               # >= 9.0
        self.assertIsNone(totals.compute_target(GBA, [dat_of(GBA_NAMES)], FILTER).coverage)

    def test_filter_without_lookup_is_an_error(self) -> None:
        with self.assertRaises(library.RatingsUnavailable):
            totals.compute_target(GBA, [dat_of(GBA_NAMES)], replace(FILTER, min_rating=5.0))

    def test_have_n_of_m_with_rating_and_owned_but_excluded(self) -> None:
        dat = dat_of(GBA_NAMES)
        prof = replace(FILTER, min_rating=7.0)
        t = totals.compute_target(GBA, [dat], prof, self.lookup)
        owned = gba_items(["Ace (USA)", "Echo (USA)", "Gust (USA)"])
        u = totals.compute_user_items(owned, GBA, prof, RatingContext(self.lookup, t.cutoff))
        out = totals.combine(t, u)
        self.assertEqual((out["target_games"], out["have_games"], out["missing_games"]), (4, 1, 3))
        self.assertEqual(out["owned_but_excluded"], 2)          # Echo (low) and Gust (unrated) are excluded by the rating
        self.assertEqual(out["owned_games"], out["have_games"] + out["owned_but_excluded"] + out["owned_incomplete"]
                         + out["owned_outside_target"])
        self.assertEqual(out["rating"]["games"], 7)
        self.assertIn("rating_coverage", out)

    def test_dat_scope_have_never_exceeds_target_when_files_are_added(self) -> None:
        dat = dat_of(GBA_NAMES)
        prof = replace(FILTER, top_n=2)
        t = totals.compute_target(GBA, [dat], prof, self.lookup)
        for names in (["Ace (USA)"], ["Ace (USA)", "Cube (USA)", "Bolt (USA)", "Dune (USA)", "Echo (USA)"]):
            u = totals.compute_user_items(gba_items(names), GBA, prof, RatingContext(self.lookup, t.cutoff))
            out = totals.combine(t, u)
            self.assertLessEqual(out["have_games"], out["target_games"])
        self.assertEqual(out["have_games"], 2)

    def test_owned_scope_target_ignores_the_cap(self) -> None:
        dat = dat_of(GBA_NAMES)
        prof = replace(FILTER, top_n=2, rank_scope="owned")
        t = totals.compute_target(GBA, [dat], prof, self.lookup)
        self.assertEqual(t.count, 5)                           # every rated game; the cap follows the user's files
        u = totals.compute_user_items(gba_items(["Dune (USA)", "Echo (USA)", "Ace (USA)"]), GBA, prof,
                                      RatingContext(self.lookup, None))
        out = totals.combine(t, u)
        self.assertEqual((out["have_games"], out["owned_but_excluded"], out["owned_outside_target"]), (2, 1, 0))

    def test_manager_passes_the_lookup_and_includes_coverage(self) -> None:
        mgr = totals.TotalsManager(lambda platform: [dat_of(GBA_NAMES)], None, lambda platform: self.lookup)
        prof = replace(FILTER, min_rating=7.0)
        mgr.request(GBA, prof, "d1")
        self.assertTrue(mgr.wait_idle(10))
        out = mgr.request(GBA, prof, "d1")
        self.assertEqual((out["target_games"], out["rating_coverage"]["rated"], out["rating"]["excluded"]), (4, 5, 3))
        # without installed ratings the worker reports an error instead of a wrong number
        mgr2 = totals.TotalsManager(lambda platform: [dat_of(GBA_NAMES)], None, lambda platform: None)
        with mock.patch("traceback.print_exc"):
            mgr2.request(GBA, prof, "d1")
            self.assertTrue(mgr2.wait_idle(10))
        self.assertIn("ratings", mgr2.request(GBA, prof, "d1")["error"])
        # no filter: no data needed, no coverage
        mgr3 = totals.TotalsManager(lambda platform: [dat_of(GBA_NAMES)], None, lambda platform: None)
        mgr3.request(GBA, FILTER, "d1")
        self.assertTrue(mgr3.wait_idle(10))
        self.assertEqual(mgr3.request(GBA, FILTER, "d1")["target_games"], 7)


# --------------------------------------------------------------------------- the updater

class FakeRatings:
    """The module surface ``autoupdate`` uses, with a controllable remote."""

    CREDIT, CREDIT_URL, REFRESH_DAYS = ratings.CREDIT, ratings.CREDIT_URL, ratings.REFRESH_DAYS
    source_date = staticmethod(ratings.source_date)
    age_days = staticmethod(ratings.age_days)
    index_path = staticmethod(ratings.index_path)
    installed = staticmethod(ratings.installed)

    def __init__(self, directory: Path) -> None:
        self.dir = directory
        self.remote = {"status": "missing", "latest": "x", "etag": "e"}
        self.checks: list[bool] = []
        self.downloads = 0
        self.fail: Exception | None = None
        self.gate: threading.Event | None = None

    def check_update(self, opener=None, directory=None, timeout=10, now=None, gate=True):
        self.checks.append(gate)
        row = dict(self.remote)
        row.setdefault("age_days", None)
        return row

    def download_and_build(self, directory=None, progress=None, cancel=None, opener=None, commit_lock=None):
        self.downloads += 1
        if progress:
            progress(1, 4, "Downloading the LaunchBox ratings")
        if self.gate is not None:
            self.gate.wait(5)
        if self.fail is not None:
            raise self.fail
        make = make_zip(self.dir.parent / "z.zip")
        if progress:
            progress(2, 4, "Building the ratings index")
        return ratings.build_index(make, self.dir, meta={"last_modified": http_date(0.3), "etag": '"n"'})


def wait_idle(mgr, timeout: float = 10.0) -> None:
    end = time.time() + timeout
    while time.time() < end:
        if not mgr.status()["running"]:
            return
        time.sleep(0.01)
    raise AssertionError("update did not finish")


class UpdaterTests(TmpDirCase):
    def setUp(self) -> None:
        super().setUp()
        self.fake = FakeRatings(paths.ratings_dir())
        self.wanted = [False]
        self.mgr = autoupdate.UpdateManager(tosec=mock.MagicMock(), nointro=mock.MagicMock(), enabled=True, whdload=None, redump=None,
                                            ratings=self.fake, ratings_wanted=lambda: self.wanted[0],
                                            state_path=self.tmp / "updates.json")
        self.mgr.enabled = True                       # (ROMORG_OFFLINE is set for the other tests)
        self.mgr.nointro.check_updates.return_value = []
        self.mgr.nointro.list_dats.return_value = []
        self.mgr.tosec.check_latest.return_value = ReleaseInfo("2025-03-13", "cat", "dl")
        self.mgr.tosec.installed_release.return_value = "2025-03-13"

    def run_startup(self) -> None:
        self.mgr._spawn()
        wait_idle(self.mgr)

    def test_nothing_happens_without_a_rating_filter(self) -> None:
        self.run_startup()
        self.assertEqual((self.fake.checks, self.fake.downloads), ([], 0))
        st = self.mgr.status()["ratings"]
        self.assertEqual((st["wanted"], st["status"], st["installed"]), (False, "absent", None))

    def test_a_startup_run_fetches_when_a_filter_is_enabled(self) -> None:
        self.wanted[0] = True
        self.mgr.nointro.check_updates.return_value = [{"name": "Nintendo - Game Boy Advance", "status": "up_to_date"}]
        self.mgr.nointro.list_dats.return_value = [mock.Mock(name="x")]
        self.run_startup()
        self.assertEqual((self.fake.checks, self.fake.downloads), ([True], 1))     # gate on at startup
        st = self.mgr.status()["ratings"]
        self.assertTrue(st["wanted"])
        self.assertEqual(st["status"], "up_to_date")
        self.assertIsNotNone(st["installed"])
        self.assertEqual(st["credit"], ratings.CREDIT)
        self.assertTrue((paths.ratings_dir() / "ratings.sqlite").is_file())

    def test_an_up_to_date_index_is_not_downloaded_again(self) -> None:
        self.wanted[0] = True
        self.fake.remote = {"status": "up_to_date", "latest": "x", "etag": "e"}
        self.build()
        self.run_startup()
        self.assertEqual((len(self.fake.checks), self.fake.downloads), (1, 0))
        self.assertEqual(self.mgr.status()["ratings"]["status"], "up_to_date")

    def test_the_button_downloads_without_a_filter_and_without_the_gate(self) -> None:
        self.assertTrue(self.mgr.request_ratings())
        wait_idle(self.mgr)
        self.assertEqual((self.fake.checks, self.fake.downloads), ([False], 1))
        self.assertEqual(self.mgr.status()["ratings"]["status"], "up_to_date")
        self.assertFalse(self.mgr.status()["error"])

    def test_progress_is_reported_while_it_runs(self) -> None:
        self.fake.gate = threading.Event()
        self.mgr.request_ratings()
        end = time.time() + 5
        while time.time() < end and self.mgr.status()["ratings"]["status"] != "updating":
            time.sleep(0.01)
        st = self.mgr.status()
        self.assertEqual((st["ratings"]["status"], st["running"], st["progress"]["source"]), ("updating", True, "ratings"))
        self.assertIn("LaunchBox", st["progress"]["message"])
        self.fake.gate.set()
        wait_idle(self.mgr)

    def test_offline_with_cache_is_quiet_and_without_cache_is_an_error(self) -> None:
        self.wanted[0] = True
        self.build()
        self.fake.remote = {"status": "error", "error": "no route"}
        self.run_startup()
        st = self.mgr.status()["ratings"]
        self.assertEqual((st["status"], st["offline"], st["error"]), ("up_to_date", True, None))
        self.assertIsNotNone(ratings.Store(paths.ratings_dir()).lookup(G, "Alpha Quest"))      # the cache is still used
        shutil.rmtree(paths.ratings_dir())
        self.run_startup()
        st = self.mgr.status()["ratings"]
        self.assertEqual(st["status"], "error")
        self.assertIn("offline", st["error"])

    def test_a_failed_download_keeps_the_old_index_and_reports_in_the_ratings_block(self) -> None:
        self.wanted[0] = True
        self.build()
        self.fake.remote = {"status": "update_available", "latest": "y", "etag": "f"}
        self.fake.fail = ratings.RatingsError("the download is not a zip file")
        before = (paths.ratings_dir() / "ratings.sqlite").read_bytes()
        self.run_startup()
        st = self.mgr.status()
        self.assertEqual((st["ratings"]["status"], st["ratings"]["error"]), ("error", "the download is not a zip file"))
        self.assertIsNone(st["error"])                       # the DAT update itself is not failed by it
        self.assertEqual((paths.ratings_dir() / "ratings.sqlite").read_bytes(), before)

    def test_a_request_during_another_update_follows_it(self) -> None:
        gate = threading.Event()

        def slow_check(timeout=None):
            gate.wait(5)
            raise urllib.error.URLError("no route")

        self.mgr.tosec.check_latest.side_effect = slow_check
        self.mgr._spawn()
        self.assertTrue(self.mgr.request_ratings())          # queued behind the running update
        gate.set()
        end = time.time() + 10
        while time.time() < end and self.fake.downloads == 0:
            time.sleep(0.02)
        wait_idle(self.mgr)
        self.assertEqual(self.fake.downloads, 1)

    def test_disabled_manager_never_fetches(self) -> None:
        self.mgr.enabled = False
        self.assertFalse(self.mgr.request_ratings())
        self.assertEqual(self.fake.downloads, 0)

    def test_other_updaters_never_touch_the_ratings_folder(self) -> None:
        self.build()
        marker = paths.ratings_dir() / "ratings.sqlite"
        before = marker.read_bytes()
        self.mgr.nointro.update_dats.return_value = {"dats": [], "failed": 0}
        self.run_startup()
        self.assertEqual(marker.read_bytes(), before)
        self.assertNotEqual(paths.ratings_dir(), paths.dats_dir())
        self.assertNotIn(paths.ratings_dir().name, {p.name for p in paths.dats_dir().iterdir()})

    def build(self) -> None:
        ratings.build_index(make_zip(self.tmp / "b.zip"), paths.ratings_dir(),
                            meta={"last_modified": http_date(1), "etag": '"b"'})


# --------------------------------------------------------------------------- the server

def _dat_xml(name: str, games: list[tuple[str, bytes]], version: str) -> str:
    lines = ['<?xml version="1.0"?>', "<datafile>",
             f"<header><name>{name}</name><description>{name}</description><version>{version}</version></header>"]
    for game, data in games:
        lines.append(f'<game name="{game}"><description>{game}</description>'
                     f'<rom name="{game}.adf" size="{len(data)}" crc="{zlib.crc32(data):08x}" '
                     f'md5="{hashlib.md5(data).hexdigest()}" sha1="{hashlib.sha1(data).hexdigest()}"/></game>')
    lines.append("</datafile>")
    return "\n".join(lines)


ALPHA, BETA, DELTA, EPS = "Alpha (1990)(Pub)", "Beta (1990)(Pub)", "Delta (1990)(Pub)", "Eps (1990)(Pub)"
SOLO = "Solo (1992)(Pub)"
AMIGA_DAT = [ALPHA, BETA, DELTA, EPS, SOLO]


class ServerTests(TmpDirCase):
    def setUp(self) -> None:
        super().setUp()
        self.root = self.tmp / "amiga"
        self.root.mkdir()
        dats = self.tmp / "data" / "dats"
        dats.mkdir(parents=True)
        rng = random.Random(5)
        self.blobs = {n: bytes(rng.getrandbits(8) for _ in range(256)) for n in AMIGA_DAT}
        (dats / f"{GAMES} (TOSEC-v2025-01-30_CM).dat").write_text(_dat_xml(GAMES, list(self.blobs.items()), "2025-01-30"))
        self.srv = server.make_server("127.0.0.1", 0, token="t", auto_update=False)
        self.port = self.srv.port
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)

    def call(self, method: str, path: str, body=None):
        headers = {"Host": f"127.0.0.1:{self.port}"}
        data = None
        if method == "POST":
            data = json.dumps(body or {}).encode()
            headers.update({"Content-Type": "application/json", "X-Romorg-Token": "t"})
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as err:
            return {"_status": err.code, **json.loads(err.read() or b"{}")}

    def save(self, **fields):
        return self.call("POST", "/api/library/profile", {"platform": "Commodore Amiga", **fields})

    def totals(self) -> dict:
        end = time.time() + 20
        while True:
            out = self.call("GET", "/api/library/totals?platform=Commodore%20Amiga")
            if not (out.get("calculating") or out.get("stale")) or time.time() > end:
                return out
            time.sleep(0.05)

    def scan(self) -> None:
        self.call("POST", "/api/scan", {"path": str(self.root), "platform": "Commodore Amiga"})
        end = time.time() + 20
        while time.time() < end:
            job = self.call("GET", "/api/job")
            if job["status"] != "running":
                self.assertEqual(job["status"], "done", job)
                return
            time.sleep(0.03)
        self.fail("scan did not finish")

    def install(self) -> None:
        ratings.build_index(make_zip(self.tmp / "s.zip"), paths.ratings_dir(), meta={"last_modified": http_date(1)})

    def put(self, *names: str) -> None:
        for n in names:
            (self.root / f"{n}.adf").write_bytes(self.blobs[n])

    # ---- fields and validation
    def test_profile_fields_roundtrip_and_defaults(self) -> None:
        info = self.call("GET", "/api/library/profile?platform=Commodore%20Amiga")
        p = info["profile"]
        self.assertEqual((p["min_rating"], p["top_n"], p["min_votes"], p["keep_unrated"], p["rank_scope"]),
                         (None, None, 5, False, "dat"))
        self.assertTrue(info["available"]["ratings"])
        out = self.save(min_rating=7.5, top_n=300, min_votes=8, keep_unrated=True, rank_scope="owned")
        p = out["profile"]
        self.assertEqual((p["min_rating"], p["top_n"], p["min_votes"], p["keep_unrated"], p["rank_scope"]),
                         (7.5, 300, 8, True, "owned"))
        again = self.call("GET", "/api/library/profile?platform=Commodore%20Amiga")["profile"]
        self.assertEqual(again, p)
        cleared = self.save(min_rating=None, top_n="")["profile"]
        self.assertEqual((cleared["min_rating"], cleared["top_n"]), (None, None))
        self.assertEqual(self.save(min_rating=0)["profile"]["min_rating"], None)       # 0 = off
        reset = self.call("POST", "/api/library/profile", {"platform": "Commodore Amiga", "reset": True})["profile"]
        self.assertEqual((reset["min_votes"], reset["keep_unrated"], reset["rank_scope"]), (5, False, "dat"))

    def test_bad_values_are_400(self) -> None:
        for body in ({"min_rating": 11}, {"min_rating": -1}, {"min_rating": "high"}, {"min_rating": True},
                     {"top_n": 0}, {"top_n": 2.5}, {"top_n": "x"}, {"top_n": -4}, {"top_n": False},
                     {"min_votes": 0}, {"min_votes": None}, {"min_votes": 1.5}, {"min_votes": "5"},
                     {"keep_unrated": "yes"}, {"keep_unrated": 1}, {"rank_scope": "everything"}, {"rank_scope": 3}):
            out = self.save(**body)
            self.assertEqual(out.get("_status"), 400, body)
        p = self.call("GET", "/api/library/profile?platform=Commodore%20Amiga")["profile"]
        self.assertEqual((p["min_rating"], p["top_n"], p["min_votes"]), (None, None, 5))      # nothing was half-saved

    def test_catalog_has_the_ratings_group(self) -> None:
        info = self.call("GET", "/api/library/profile?platform=Commodore%20Amiga")
        grp = [e["id"] for e in info["catalog"] if e.get("group") == "ratings"]
        self.assertEqual(grp, ["min_rating", "top_n", "min_votes", "keep_unrated", "rank_scope"])
        self.assertIn("rating_unrated", info["reason_labels"])

    # ---- without data
    def test_filter_without_data_is_pending_never_silent(self) -> None:
        self.put(ALPHA, BETA)
        self.scan()
        self.save(min_rating=7.0)
        plan = self.call("POST", "/api/library/plan", {"limit": 5})
        self.assertEqual(plan["_status"], 409)
        self.assertEqual(plan["code"], "ratings_pending")
        self.assertIn("ratings data", plan["error"])
        out = self.call("GET", "/api/library/totals?platform=Commodore%20Amiga")
        self.assertTrue(out["ratings_pending"])
        self.assertIsNone(out["target_games"])
        self.assertEqual(self.call("POST", "/api/library/apply", {})["_status"], 409)
        self.save(min_rating=None)                           # no filter again: no data needed
        self.assertIn("files", self.call("POST", "/api/library/plan", {"limit": 5}))

    def test_ratings_endpoints(self) -> None:
        st = self.call("GET", "/api/ratings")
        self.assertEqual((st["status"], st["installed"], st["credit"]), ("absent", None, ratings.CREDIT))
        self.assertIn("Commodore Amiga", st["supported"])
        dl = self.call("POST", "/api/ratings/download", {})
        self.assertFalse(dl["started"])                      # updates are off in this test run
        self.install()
        st = self.call("GET", "/api/ratings")
        self.assertEqual(st["status"], "up_to_date")
        self.assertIn("ratings", self.call("GET", "/api/updates"))

    # ---- with an installed index
    def test_plan_and_totals_with_a_rating_filter(self) -> None:
        self.install()
        self.put(ALPHA, BETA, DELTA, EPS, SOLO)
        self.scan()
        base = self.totals()
        self.assertEqual(base["target_games"], 5)
        self.assertEqual((base["rating_coverage"]["games"], base["rating_coverage"]["rated"]), (5, 3))   # Delta: 3 votes
        self.save(min_rating=7.0)
        out = self.totals()
        self.assertEqual((out["target_games"], out["have_games"]), (2, 2))                              # Alpha 9.0, Eps 7.0
        self.assertEqual(out["owned_but_excluded"], 3)
        self.assertFalse(out["ratings_pending"])
        plan = self.call("POST", "/api/library/plan", {"limit": 50})
        self.assertEqual(plan["reasons"]["excluded_rating_low"], 1)                                      # Beta 6.0
        self.assertEqual(plan["reasons"]["excluded_rating_unrated"], 2)                                  # Delta, Solo
        self.assertEqual(plan["rating"]["excluded"], 3)
        self.assertEqual(plan["rating"]["min_rating"], 7.0)
        self.assertIn(plan["exclusions"]["exclusive"].get("rating_low"), (1,))
        why = self.call("POST", "/api/library/plan", {"limit": 50, "why": "rating_low"})
        self.assertEqual([i["from"] for i in why["items"]], [f"{BETA}.adf"])
        self.assertTrue(why["items"][0]["to"].startswith("_excluded/"))
        van = self.call("POST", "/api/library/vanished", {"limit": 50})
        self.assertEqual(van["by_reason"], {"rating_low": 1, "rating_unrated": 2})
        item = next(i for i in van["items"] if i["reason"] == "rating_low")
        self.assertIn("lower the minimum rating", item["hint"])
        self.save(keep_unrated=True)
        out = self.totals()
        self.assertEqual((out["target_games"], out["owned_but_excluded"]), (4, 1))
        self.save(top_n=1, min_rating=None, keep_unrated=False)
        out = self.totals()
        self.assertEqual((out["target_games"], out["have_games"]), (1, 1))
        self.save(rank_scope="owned")
        self.assertEqual(self.totals()["rank_scope"], "owned")

    def test_plan_id_changes_with_the_rating_options(self) -> None:
        self.install()
        self.put(ALPHA, BETA)
        self.scan()
        a = self.call("POST", "/api/library/plan", {"limit": 1})["plan_id"]
        self.save(min_rating=8.0)
        b = self.call("POST", "/api/library/plan", {"limit": 1})["plan_id"]
        self.assertNotEqual(a, b)
        self.assertEqual(self.call("POST", "/api/library/apply", {"plan_id": a})["_status"], 409)

    def test_browse_games_rating_column_filter_and_sort(self) -> None:
        self.put(ALPHA)
        self.scan()
        rows = self.call("GET", "/api/scan/results?kind=games&limit=50")["items"]
        self.assertTrue(all(r["rating"] is None for r in rows))                  # no data installed: dashes
        self.install()
        rows = self.call("GET", "/api/scan/results?kind=games&limit=50")["items"]
        by = {r["title"]: r for r in rows}
        self.assertEqual((by["Alpha"]["rating"], by["Alpha"]["votes"], by["Alpha"]["rating_match"]), (9.0, 100, "exact"))
        self.assertIsNone(by["Solo"]["rating"])
        self.assertIsNone(by["Delta"]["rating"] if by["Delta"]["votes"] is None else None)
        rated = self.call("GET", "/api/scan/results?kind=games&rated=1&limit=50")
        self.assertEqual({r["title"] for r in rated["items"]}, {"Alpha", "Beta", "Delta", "Eps"})
        unrated = self.call("GET", "/api/scan/results?kind=games&rated=0&limit=50")
        self.assertEqual({r["title"] for r in unrated["items"]}, {"Solo"})
        ordered = self.call("GET", "/api/scan/results?kind=games&sort=rating&limit=50")["items"]
        self.assertEqual([r["title"] for r in ordered][:4], ["Alpha", "Delta", "Eps", "Beta"])   # 9.0, 8.0, 7.0, 6.0; Solo last
        self.assertEqual(ordered[-1]["title"], "Solo")


# --------------------------------------------------------------------------- the UI contract

class UiContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.html = server.read_static("index.html").decode()
        self.js = server.read_static("app.js").decode()
        self.css = server.read_static("style.css").decode()

    def test_no_hard_coded_rating_rule_list_in_the_script(self) -> None:
        for rule in ('"min_rating"', '"top_n"', '"min_votes"', '"keep_unrated"', "'min_rating'", "'top_n'", "'keep_unrated'"):
            self.assertNotIn(rule, self.js, rule)
        for needle in ('e.group === "ratings"', "ratingEntries", "e.filter", "e.summary", "e.choices", "e.kind === \"choice\"",
                       "/api/ratings/download", "info.reason_labels", "rating_codes", 'class: "rating-rows"'):
            self.assertIn(needle, self.js, needle)

    def test_ratings_ui_pieces(self) -> None:
        for needle in ("Ratings found for", "LaunchBox data from", "Download ratings",
                       "Games with no rating are excluded while a rating filter is set (tick Keep unrated games to keep them).",
                       "Ratings: LaunchBox Games Database community ratings", "target games are rated", "Preview and Build wait for it",
                       "retryPending", "ratings_pending", "Unrated", "Rated", "th-sort", "rating-cell", "ratingCell"):
            self.assertIn(needle, self.js, needle)
        self.assertIn('id="lib-ratings-banner"', self.html)
        for sel in (".rating-rows", ".rating-input", ".ratings-status", ".ratings-banner", ".th-sort"):
            self.assertIn(sel, self.css)

    def test_every_dollar_id_exists(self) -> None:
        import re
        ids = set(re.findall(r'\$\("([^"$`]+)"\)', self.js))
        missing = [i for i in ids if f'id="{i}"' not in self.html and f'id: "{i}"' not in self.js]
        self.assertEqual(missing, [])



if __name__ == "__main__":
    unittest.main()
