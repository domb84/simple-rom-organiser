"""CD32 / CDTV discs as CHDs (romorg.cdconvert): WAVE tracks in cue sheets, a CHD told by the files it rebuilds, the conversion on
Build library (checked against the DAT, originals deleted or kept) and the discs the rules set aside left as they are."""

from __future__ import annotations

import hashlib
import json
import os
import random
import shutil
import struct
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import zlib
from pathlib import Path
from typing import Any
from unittest import mock

os.environ.setdefault("ROMORG_RETROARCH_DETECT", "0")

from romorg import cdconvert, cdimage, chdwrite, scanner, server  # noqa: E402
from romorg.datfile import parse_dat  # noqa: E402

ISO_DAT = "Commodore Amiga CD32 - Games - [ISO]"
BIN_DAT = "Commodore Amiga CD32 - Games - [BIN]"
PLAT = "Commodore Amiga"


def wav(samples: bytes, extra: bytes = b"") -> bytes:
    """A CD audio .wav of ``samples``; ``extra``: chunks between the format and the data (a LIST chunk ...)."""
    return (b"RIFF" + struct.pack("<I", 36 + len(extra) + len(samples)) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 2, 44100, 176400, 4, 16)
            + extra + b"data" + struct.pack("<I", len(samples)) + samples)


def iso_disc(game: str, seed: int, audio: int = 2, extra: bytes = b"") -> dict[str, bytes]:
    """The files of a TOSEC [ISO] disc: a cue, its data track (.iso) and ``audio`` .wav tracks."""
    rng = random.Random(seed)
    blob = lambda n: bytes(rng.getrandbits(8) for _ in range(n))  # noqa: E731
    n = audio + 1
    iso = f"{game}[Track 01 of {n:02d}].iso"
    files = {iso: blob(40 * 2048)}
    cue = [f'FILE "{iso}" BINARY', "  TRACK 01 MODE1/2048", "    INDEX 01 00:00:00"]
    for t in range(2, n + 1):
        name = f"{game}[Track {t:02d} of {n:02d}].wav"
        files[name] = wav(blob((20 + t) * 2352), extra if t == 2 else b"")
        cue += [f'FILE "{name}" WAVE', f"  TRACK {t:02d} AUDIO"] + (["    PREGAP 00:02:00"] if t == 2 else []) + ["    INDEX 01 00:00:00"]
    files[game + ".cue"] = ("\r\n".join(cue) + "\r\n").encode()
    return files


def bin_disc(game: str, seed: int) -> dict[str, bytes]:
    """A TOSEC [BIN] disc: one .bin holding a MODE1/2352 track and an audio track."""
    rng = random.Random(seed)
    data = bytes(rng.getrandbits(8) for _ in range(30 * 2352))
    audio = bytes(rng.getrandbits(8) for _ in range(25 * 2352))
    cue = (f'FILE "{game}.bin" BINARY\r\n  TRACK 01 MODE1/2352\r\n    INDEX 01 00:00:00\r\n  TRACK 02 AUDIO\r\n'
           f"    INDEX 01 00:00:30\r\n")
    return {game + ".bin": data + audio, game + ".cue": cue.encode()}


def write_dat(folder: Path, dat: str, discs: dict[str, dict[str, bytes]]) -> Path:
    lines = ['<?xml version="1.0"?>', "<datafile>", f"<header><name>{dat}</name><description>{dat}</description><version>2019-09-07</version></header>"]
    for game, files in discs.items():
        lines.append(f'<game name="{game}"><description>{game}</description>')
        for name, data in sorted(files.items()):
            lines.append(f'<rom name="{name.replace("&", "&amp;")}" size="{len(data)}" crc="{zlib.crc32(data):08x}" '
                         f'md5="{hashlib.md5(data).hexdigest()}" sha1="{hashlib.sha1(data).hexdigest()}"/>')
        lines.append("</game>")
    lines.append("</datafile>")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{dat} (TOSEC-v2019-09-07_CM).dat"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def put_files(folder: Path, files: dict[str, bytes]) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        (folder / name).write_bytes(data)


def put_zip(path: Path, files: dict[str, bytes]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return path


class Scratch(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="romorg-cd-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)


class WaveTracksTests(Scratch):
    def test_the_samples_of_a_wave_file_are_its_data_chunk(self) -> None:
        samples = bytes(range(147)) * 48
        p = self.tmp / "a.wav"
        p.write_bytes(wav(samples))
        self.assertEqual(cdimage.wave_data(p), (44, len(samples)))
        p.write_bytes(wav(samples, b"LIST" + struct.pack("<I", 5) + b"abcde\0"))      # (an odd chunk: padded to even)
        self.assertEqual(cdimage.wave_data(p), (44 + 14, len(samples)))

    def test_a_wave_file_that_is_not_cd_audio_is_refused(self) -> None:
        p = self.tmp / "a.wav"
        mono = (b"RIFF" + struct.pack("<I", 36 + 2352) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, 44100, 88200, 2, 16)
                + b"data" + struct.pack("<I", 2352) + bytes(2352))
        for data, why in ((mono, "not CD audio"), (wav(bytes(2352 + 4)), "whole number"), (b"RIFX" + bytes(40), "not a WAVE")):
            p.write_bytes(data)
            with self.assertRaisesRegex(cdimage.ImageError, why):
                cdimage.wave_data(p)

    def test_a_cue_with_wave_tracks_is_laid_out_like_binary_audio(self) -> None:
        files = iso_disc("G (1994)(P)", 1)
        put_files(self.tmp, files)
        tracks = cdimage.parse_cue(self.tmp / "G (1994)(P).cue")
        self.assertEqual([(t.type, t.offset, t.file_frames, t.pregap) for t in tracks],
                         [("MODE1", 0, 40, 0), ("AUDIO", 44, 22, 150), ("AUDIO", 44, 23, 0)])
        bad = self.tmp / "bad.cue"
        bad.write_text('FILE "G (1994)(P)[Track 02 of 03].wav" WAVE\n  TRACK 01 MODE1/2352\n    INDEX 01 00:00:00\n')
        with self.assertRaisesRegex(cdimage.ImageError, "data in a WAVE"):
            cdimage.parse_cue(bad)


class IdentifyTests(Scratch):
    def chd_of(self, files: dict[str, bytes], game: str) -> Path:
        put_files(self.tmp / "src", files)
        out = self.tmp / (game + ".chd")
        chdwrite.write_chd(out, cdimage.open_image(self.tmp / "src" / (game + ".cue")))
        return out

    def test_a_chd_is_the_disc_whose_files_it_rebuilds(self) -> None:
        iso_g, bin_g = "Iso Game (1994)(Pub)[!]", "Bin Game (1995)(Pub)"
        discs = {iso_g: iso_disc(iso_g, 2), bin_g: bin_disc(bin_g, 3), "Other (1993)(Pub)": iso_disc("Other (1993)(Pub)", 4)}
        dat = parse_dat(write_dat(self.tmp / "dats", ISO_DAT, discs))
        for game in (iso_g, bin_g):
            h = cdconvert.hash_chd(self.chd_of(discs[game], game))
            roms = cdconvert.identify(h, [dat])
            self.assertEqual(sorted(r.name for r in roms), sorted(discs[game]), game)      # (its cue too: the CHD holds the layout)
        self.assertTrue(cdconvert.is_cd_chd(self.tmp / (iso_g + ".chd")))

    def test_a_wave_file_with_more_than_the_standard_header_cannot_be_rebuilt(self) -> None:
        g = "Long Header (1994)(Pub)"
        files = iso_disc(g, 5, extra=b"LIST" + struct.pack("<I", 4) + b"INFO")
        dat = parse_dat(write_dat(self.tmp / "dats", ISO_DAT, {g: files}))
        h = cdconvert.hash_chd(self.chd_of(files, g))
        self.assertIsNone(cdconvert.identify(h, [dat]))

    def test_what_cannot_be_converted_and_checked_says_why(self) -> None:
        g = "X (1994)(P)"
        dat = parse_dat(write_dat(self.tmp / "dats", ISO_DAT, {
            "mp3": {g + ".cue": b"c", g + "[Track 2].mp3": b"m", g + "[Track 1].iso": bytes(2048)},
            "odd": {g + ".cue": b"c", g + "[Track 1].iso": bytes(2048), g + "[Track 2].wav": bytes(44 + 2352 + 10)},
            "nocue": {g + ".ccd": b"c", g + ".img": bytes(2352), g + ".sub": bytes(96)},
            "iso": {g + ".iso": bytes(2048)}}))
        games = dat.games()
        self.assertIn(".mp3", cdconvert.convertible_reason(games["mp3"]))
        self.assertIn("not plain CD audio", cdconvert.convertible_reason(games["odd"]))
        self.assertIn("no cue sheet", cdconvert.convertible_reason(games["nocue"]))
        self.assertEqual(cdconvert.convertible_reason(games["iso"]), "")


class DiscCase(Scratch):
    """One [ISO] disc with WAV tracks and its DAT."""

    def setUp(self) -> None:
        super().setUp()
        self.game = "Disc Game (1994)(Pub)[!]"
        self.files = iso_disc(self.game, 7)
        self.dat = parse_dat(write_dat(self.tmp / "dats", ISO_DAT, {self.game: self.files}))
        self.root = self.tmp / "roms"
        self.root.mkdir()
        self.cache = self.tmp / "hashes.sqlite"

    def scan(self) -> Any:
        return scanner.scan(self.root, [self.dat], containers=("chd",), cache_path=self.cache)

    def convert(self, delete: bool) -> tuple[list, dict]:
        res = self.scan()
        ops = cdconvert.plan_conversions(res, [ISO_DAT])
        for op in ops:
            op.delete_originals = delete
        return ops, cdconvert.apply_conversions(ops, self.root)


class ScanAndConvertTests(DiscCase):
    def test_the_scan_matches_a_chd_by_its_files_and_remembers_them(self) -> None:
        put_files(self.tmp / "src", self.files)
        chdwrite.write_chd(self.root / "any name.chd", cdimage.open_image(self.tmp / "src" / (self.game + ".cue")))
        res = self.scan()
        self.assertEqual(sorted(m.entry.member for m in res.matched), sorted(self.files))
        self.assertEqual({(m.container, m.dat) for m in res.matched}, {("chd", ISO_DAT)})
        self.assertEqual((res.missing, res.unmatched), ([], []))
        with mock.patch.object(cdconvert, "hash_chd", side_effect=AssertionError("decoded again")):
            self.assertEqual(len(self.scan().matched), len(self.files))          # (from the hash cache)
        plain = scanner.scan(self.root, [self.dat], cache_path=self.cache)       # (a system without CD discs: a file like any other)
        self.assertEqual(len(plain.unmatched), 1)

    def test_a_zip_of_a_disc_becomes_a_checked_chd_and_the_zip_is_deleted(self) -> None:
        put_zip(self.root / "in" / "disc.zip", self.files)
        ops, out = self.convert(delete=True)
        self.assertEqual([(op.status, op.archive, op.dst.name) for op in ops], [("convert", True, self.game + ".chd")])
        self.assertEqual((out["converted"], out["failed"], out["deleted"]), (1, [], 1))
        self.assertEqual(sorted(p.name for p in self.root.rglob("*") if p.is_file() and not p.name.startswith(".romorg")),
                         [self.game + ".chd"])
        res = self.scan()
        self.assertEqual(sorted(m.entry.member for m in res.matched), sorted(self.files))

    def test_kept_originals_go_to_converted_originals(self) -> None:
        put_files(self.root / "loose", self.files)
        ops, out = self.convert(delete=False)
        self.assertEqual([(op.status, op.archive) for op in ops], [("convert", False)])
        self.assertEqual((out["converted"], out["deleted"]), (1, 0))
        self.assertTrue((self.root / "loose" / (self.game + ".chd")).is_file())
        kept = sorted(p.name for p in (self.root / "_converted_originals" / "loose").iterdir())
        self.assertEqual(kept, sorted(self.files))
        res = self.scan()
        self.assertEqual(cdconvert.plan_conversions(res, [ISO_DAT]), [])                 # (the originals kept are not converted again)

    def test_a_chd_that_does_not_check_out_changes_nothing(self) -> None:
        put_zip(self.root / "disc.zip", self.files)
        res = self.scan()
        ops = cdconvert.plan_conversions(res, [ISO_DAT])
        ops[0].delete_originals = True
        with mock.patch.object(cdconvert, "matches_game", return_value=False):
            out = cdconvert.apply_conversions(ops, self.root)
        self.assertEqual(out["converted"], 0)
        self.assertIn("verification failed", out["failed"][0]["error"])
        self.assertEqual(sorted(p.name for p in self.root.rglob("*") if p.is_file() and not p.name.startswith(".romorg")), ["disc.zip"])

    def test_a_disc_the_rules_set_aside_and_an_incomplete_one_are_not_converted(self) -> None:
        put_zip(self.root / "a.zip", self.files)
        part = dict(self.files)
        part.pop(next(n for n in part if n.endswith(".wav")))
        put_zip(self.root / "b.zip", part)
        ops = cdconvert.plan_conversions(self.scan(), [ISO_DAT], keep=lambda p: p.name != "a.zip")
        self.assertEqual([(op.src.name, op.status) for op in ops], [("a.zip", "skip")])
        self.assertIn("set this disc aside", ops[0].reason)


class SafetyTests(DiscCase):
    """What the conversion must never do: lose a checked CHD, delete an archive that holds anything but the disc, touch a disc the rules
    set aside, or say originals are deleted where they never are."""

    def test_an_original_that_cannot_be_deleted_never_costs_the_checked_chd(self) -> None:
        put_files(self.root / "loose", self.files)
        real = os.unlink
        stuck = next(n for n in self.files if n.endswith(".wav"))

        def unlink(path: Any, *a: Any, **k: Any) -> None:
            if os.path.basename(path) == stuck:
                raise PermissionError(13, "in use", str(path))
            real(path, *a, **k)
        with mock.patch("os.unlink", side_effect=unlink):
            _ops, out = self.convert(delete=True)
        self.assertEqual((out["converted"], out["failed"], len(out["not_deleted"])), (1, [], 1))
        left = sorted(p.name for p in (self.root / "loose").iterdir())
        self.assertEqual(left, sorted([self.game + ".chd", stuck]))                     # (the CHD stays; the stuck file is reported)

    def test_an_archive_that_holds_anything_but_the_disc_is_never_converted(self) -> None:
        adf = bytes(range(256)) * 4
        other = parse_dat(write_dat(self.tmp / "dats2", "Commodore Amiga - Games - [ADF]", {"Other (1990)(X)": {"Other (1990)(X).adf": adf}}))
        put_zip(self.root / "with adf.zip", {**self.files, "Other (1990)(X).adf": adf})
        put_zip(self.root / "with readme.zip", {**self.files, "readme.txt": b"hello"})
        res = scanner.scan(self.root, [self.dat, other], containers=("chd",), cache_path=self.cache)
        self.assertEqual(cdconvert.plan_conversions(res, [ISO_DAT]), [])
        op = cdconvert.plan_conversions(self.scan(), [ISO_DAT])                          # (and checked again right before converting)
        self.assertEqual(op, [])
        put_zip(self.root / "disc.zip", self.files)
        ops = cdconvert.plan_conversions(self.scan(), [ISO_DAT])
        with zipfile.ZipFile(self.root / "disc.zip", "a") as zf:                          # (something added after the scan)
            zf.writestr("late.txt", b"x")
        ops[0].delete_originals = True
        out = cdconvert.apply_conversions(ops, self.root)
        self.assertIn("holds other files too", out["failed"][0]["error"])
        self.assertTrue((self.root / "disc.zip").is_file())

    def test_a_disc_in_a_reason_folder_is_not_converted(self) -> None:
        put_zip(self.root / "_excluded" / "disc.zip", self.files)
        self.assertEqual(cdconvert.plan_conversions(self.scan(), [ISO_DAT]), [])

    def test_systems_that_never_delete_originals_do_not_say_they_do(self) -> None:
        from romorg import library, platforms
        cfg: dict = {}
        self.assertTrue(library.app_defaults().convert_first and library.app_defaults().delete_converted)
        for name, delete in (("Super Nintendo Entertainment System", False), ("Nintendo 64", False), ("Commodore Amiga", True),
                             ("Sony PlayStation", True), ("Nintendo Game Boy Advance", False)):
            prof = library.load_profile(cfg, platforms.get_platform(name))
            self.assertEqual(prof.delete_converted, delete, name)


class UnpackAheadTests(Scratch):
    """The next discs are unpacked in threads while one is compressed; no work folder stays behind, whatever happens."""

    def test_several_discs_convert_and_leave_no_work_folders(self) -> None:
        discs = {f"Disc {i} (1994)(Pub)": iso_disc(f"Disc {i} (1994)(Pub)", 30 + i) for i in range(4)}
        dat = parse_dat(write_dat(self.tmp / "dats", ISO_DAT, discs))
        root = self.tmp / "roms"
        for i, (g, files) in enumerate(discs.items()):
            put_zip(root / f"d{i}.zip", files)
        res = scanner.scan(root, [dat], containers=("chd",), cache_path=self.tmp / "h.sqlite")
        ops = cdconvert.plan_conversions(res, [ISO_DAT])
        real = cdconvert.matches_game
        with mock.patch.object(cdconvert, "matches_game", side_effect=lambda h, roms: real(h, roms) and "Disc 2" not in roms[0].game):
            out = cdconvert.apply_conversions(ops, root)
        self.assertEqual((out["converted"], len(out["failed"])), (3, 1))
        left = sorted(p.name for p in root.iterdir() if p.is_file() and not p.name.startswith(".romorg"))
        self.assertEqual(left, ["Disc 0 (1994)(Pub).chd", "Disc 1 (1994)(Pub).chd", "Disc 3 (1994)(Pub).chd", "d2.zip"])   # (the failed one stays)
        self.assertFalse([p for p in root.rglob("*") if "unpack" in p.name])


class BuildLibraryTests(Scratch):
    """Build library on the Amiga with "Convert when building" and "Delete the originals after converting": the kept disc becomes a CHD
    where the game goes, the zip is deleted; a disc the rules exclude is archived as it is."""

    def setUp(self) -> None:
        super().setUp()
        env = mock.patch.dict(os.environ, {"ROMORG_DATA_DIR": str(self.tmp / "data"), "ROMORG_OFFLINE": "1"})
        env.start()
        self.addCleanup(env.stop)
        self.good, self.bad = "Good Disc (1994)(Pub)[!]", "Bad Disc (1994)(Pub)[b]"
        self.discs = {self.good: iso_disc(self.good, 11), self.bad: iso_disc(self.bad, 12, audio=1)}
        write_dat(self.tmp / "data" / "dats", ISO_DAT, self.discs)
        self.root = self.tmp / "amiga"
        put_zip(self.root / "incoming" / "good.zip", self.discs[self.good])
        put_zip(self.root / "incoming" / "bad.zip", self.discs[self.bad])
        self.srv = server.make_server("127.0.0.1", 0, token="t", auto_update=False)
        self.port = self.srv.port
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)

    def call(self, path: str, body: Any = None) -> Any:
        method = "GET" if body is None else "POST"
        headers = {"Host": f"127.0.0.1:{self.port}"}
        data = None
        if method == "POST":
            data = json.dumps(body).encode()
            headers.update({"Content-Type": "application/json", "X-Romorg-Token": "t"})
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as err:
            self.fail(f"{path}: {err.code} {err.read()!r}")

    def job(self) -> dict[str, Any]:
        deadline = time.time() + 60
        while time.time() < deadline:
            j = self.call("/api/job")
            if j and j.get("status") != "running":
                return j
            time.sleep(0.05)
        self.fail("job did not finish")

    def test_build_converts_the_kept_disc_and_archives_the_other(self) -> None:
        self.call("/api/scan", {"path": str(self.root), "platform": PLAT})
        self.assertEqual(self.job()["status"], "done")
        from romorg import library               # (the bad disc is excluded: every rule on - bad dumps are allowed by default)
        self.call("/api/library/profile", {"platform": PLAT, "convert_first": True, "delete_converted": True,
                                           "exclude": sorted(library.RULES)})
        aside = self.tmp / "rom-archive"
        plan = self.call("/api/library/plan", {"aside_to": str(aside)})
        self.assertEqual(plan["convert"], {"count": 1, "kind": "chd", "archives": 1, "delete": True})
        rows = [r for r in plan["items"] if r.get("from", "").endswith("good.zip")]
        self.assertEqual(rows[0]["to_name"], self.good + ".chd")
        self.call("/api/library/apply", {"aside_to": str(aside), "plan_id": plan["plan_id"]})
        j = self.job()
        self.assertEqual(j["status"], "done", j)
        files = sorted(p.relative_to(self.root).as_posix() for p in self.root.rglob("*") if p.is_file() and not p.name.startswith(".romorg"))
        self.assertEqual(files, [f"{ISO_DAT}/{self.good}.chd"])
        self.assertEqual([p.name for p in aside.rglob("*.zip")], ["bad.zip"])            # (excluded: archived as it is, not converted)
        totals = self.call(f"/api/library/totals?platform={urllib.parse.quote(PLAT)}")
        for _ in range(200):
            if not totals.get("calculating"):
                break
            time.sleep(0.05)
            totals = self.call(f"/api/library/totals?platform={urllib.parse.quote(PLAT)}")
        self.assertEqual(totals.get("have_games"), 1, totals)




if __name__ == "__main__":
    unittest.main()
