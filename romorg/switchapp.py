"""The Switch page's backend: where the games and the two emulators' data are, one scan of all of it, the checksum check, the tidy plan
and the rows the page shows.

The scan has two halves that meet at the title ID: the game files (``switchscan``) and the save folders of Eden (or a yuzu fork)
and Ryujinx (``switchsaves``). A game with a file but no save, a save with no file here, and a save that only one emulator has are
all shown. Nothing here changes anything on disk.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import switchdb, switchfmt, switchkeys, switchsaves, switchscan, switchverify

__all__ = ["header_key", "normalise", "describe", "scan_all", "rows", "verify_all", "default_archive"]

TEXT_KEYS = ("games", "eden", "ryujinx", "keys", "archive", "off")


def normalise(raw: Any) -> Dict[str, Any]:
    """The saved Switch settings: ``games`` (the folder with the game files), ``eden`` (its NAND folder), ``ryujinx`` (its data
    folder), ``keys`` (a ``prod.keys``, optional) and ``off`` (the emulators switched off). The games folder is the system's own folder
    (the platform's); what was set here before is only a fallback."""
    raw = raw if isinstance(raw, dict) else {}
    out: Dict[str, Any] = {k: str(raw.get(k) or "") for k in TEXT_KEYS}
    out["verify_scan"] = bool(raw.get("verify_scan"))             # a scan also checks every file's checksums (reads all the files)
    return out


def _path(text: str) -> Optional[Path]:
    return Path(os.path.abspath(os.path.expanduser(text))) if text.strip() else None


def default_archive(games: str, common: str = "") -> str:
    """The archive folder of the tidy when this page has none of its own: the one in Settings. There is no default beyond that:
    with none set, what the rules set aside stays in the games folder (``_superseded`` ...)."""
    return common


def effective(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """The settings as they are used: what the user chose, and for what they left empty what was found on this machine - Eden's NAND
    folder, a games folder from Eden's or Ryujinx's own settings, and Ryujinx's data folder (a portable one next to the games first).
    ``auto`` names the values that were found."""
    out = dict(cfg)
    auto: List[str] = []
    eden = switchsaves.detect_eden()
    off = {x for x in out["off"].split(",") if x}                    # emulators switched off on the Emulators page
    if "eden" in off:
        out["eden"] = ""
    elif not out["eden"].strip() and eden.get("nand"):
        out["eden"] = str(eden["nand"])
        auto.append("eden")
    if not out["games"].strip():
        ryu0 = switchsaves.detect_ryujinx()
        for g in [*(eden.get("games") or []), *(ryu0.get("games") or [])]:
            if Path(g).is_dir():
                out["games"] = g
                auto.append("games")
                break
    if "ryujinx" in off:
        out["ryujinx"] = ""
    elif not out["ryujinx"].strip():
        ryu = switchsaves.detect_ryujinx(near=[Path(out["games"])] if out["games"].strip() else [])
        if ryu.get("data"):
            out["ryujinx"] = str(ryu["data"])
            auto.append("ryujinx")
    out["auto"] = auto
    return out


def store(key: str, value: str, cfg: Dict[str, Any]) -> None:
    """Remember what the user typed; a value that is the one found anyway is stored as "" (it stays automatic)."""
    cfg[key] = value
    if value and key in ("games", "eden", "ryujinx"):
        eff = effective({**cfg, key: ""})
        if key in eff["auto"] and os.path.normcase(os.path.normpath(eff[key])) == os.path.normcase(os.path.normpath(value)):
            cfg[key] = ""


def describe(cfg: Dict[str, Any], last: Optional[Dict[str, Any]], common_archive: str = "") -> Dict[str, Any]:
    """Everything the page needs before a scan: the settings (as used), what was found on this machine, the database and the keys."""
    eff = effective(cfg)
    keys = _path(cfg["keys"])
    found = switchkeys.find_prod_keys()
    shown = {k: v for k, v in eff.items() if k != "auto"}
    return {"config": shown, "auto": eff["auto"],
            "detected": {"eden": switchsaves.detect_eden(), "ryujinx": switchsaves.detect_ryujinx(near=[Path(eff["games"])] if eff["games"].strip() else [])},
            "db": switchdb.db_info(),
            "keys": {"path": str(keys) if keys and keys.is_file() else (str(found) if found else ""),
                     "chosen": bool(keys and keys.is_file())},
            "archive": cfg["archive"] or default_archive(cfg["games"], common_archive),
            "archive_is_common": not cfg["archive"],
            "scan": last["summary"] if last else None}


def header_key(cfg: Dict[str, Any]) -> Optional[bytes]:
    keys_path = _path(cfg["keys"])
    if keys_path is None or not keys_path.is_file():
        keys_path = switchkeys.find_prod_keys()
    return switchkeys.load_keys(keys_path).get("header_key") if keys_path else None


def _save_entry(items: List[switchsaves.SwitchSave]) -> Optional[Dict[str, Any]]:
    items = [s for s in items if s.files]
    if not items:
        return None
    return {"count": len(items), "files": sum(s.files for s in items), "bytes": sum(s.bytes for s in items),
            "mtime": max(s.mtime for s in items), "items": [s.public() for s in items]}


def scan_all(cfg: Dict[str, Any], progress: Callable[[int, int, str], None], cancel: Any) -> Dict[str, Any]:
    """Scan the games folder and read both emulators' saves; the result is what ``rows`` pages through."""
    t0 = time.time()
    db = switchdb.SwitchDb()
    key = header_key(cfg)
    verdicts = switchverify.VerifyCache()
    games = switchscan.SwitchScan(Path(cfg["games"] or "."))
    if cfg["games"].strip():
        folder = _path(cfg["games"])
        if folder is None or not folder.is_dir():
            db.close()
            verdicts.close()
            raise FileNotFoundError(f"The games folder does not exist: {cfg['games']}")
        games = switchscan.scan_folder(folder, db, key, progress, cancel, verdicts)
    verdicts.close()
    progress(0, 0, "Reading the save data...")
    eden = switchsaves.find_eden(_path(cfg["eden"])) if cfg["eden"].strip() else []
    ryu = switchsaves.find_ryujinx(_path(cfg["ryujinx"])) if cfg["ryujinx"].strip() else []
    names: Dict[str, str] = {g.base_id: g.name for g in games.games.values()}
    saves: List[Dict[str, Any]] = []
    empty_titles = 0
    for title, items in switchsaves.group_by_title(eden, ryu).items():
        e_items = [s for s in items if s.emulator == "eden"]
        r_items = [s for s in items if s.emulator == "ryujinx" and s.kind in ("account", "device")]
        e, r = _save_entry(e_items), _save_entry(r_items)
        if e is None and r is None:
            empty_titles += 1
            continue
        pair_e = next((s for s in e_items if s.kind == "account" and s.files), None)
        pair_r = next((s for s in r_items if s.kind == "account" and s.files), None)
        base = switchfmt.base_id(title)
        known = db.title(base) or db.title(title)
        saves.append({"title_id": title, "base_id": base, "name": names.get(base) or (known["name"] if known else ""),
                      "has_game": base in games.games, "eden": e, "ryujinx": r,
                      "total": (e["count"] if e else 0) + (r["count"] if r else 0),
                      "state": switchsaves.describe_pair(pair_e, pair_r)})
    saves.sort(key=lambda s: ((s["name"] or s["title_id"]).casefold(), s["title_id"]))
    rows_games = sorted((g.public() for g in games.games.values()), key=lambda g: g["name"].casefold())
    for g in rows_games:
        mine = [s for s in saves if s["base_id"] == g["title_id"]]
        g["saves"] = {"eden": sum(s["eden"]["count"] for s in mine if s["eden"]), "ryujinx": sum(s["ryujinx"]["count"] for s in mine if s["ryujinx"])}
    summary = {
        "at": time.time(), "seconds": round(time.time() - t0, 1), "games_folder": cfg["games"], "files": games.files, "bytes": games.bytes,
        "games": len(rows_games), "unidentified": len(games.unidentified),
        "database": db.available, "keys": bool(key),
        "checksums": {"ok": sum(1 for g in games.games.values() for f in g.files if f.checksums == "ok"),
                      "damaged": sum(1 for g in games.games.values() for f in g.files if f.checksums == "damaged"),
                      "unchecked": sum(1 for g in games.games.values() for f in g.files if not f.checksums)},
        "saves": {"eden": sum(s["eden"]["count"] for s in saves if s["eden"]), "ryujinx": sum(s["ryujinx"]["count"] for s in saves if s["ryujinx"]),
                  "total": sum(s["total"] for s in saves), "titles": len(saves), "both": sum(1 for s in saves if s["eden"] and s["ryujinx"]),
                  "same": sum(1 for s in saves if s["state"] == "same"), "empty_titles": empty_titles,
                  "without_game": sum(1 for s in saves if not s["has_game"])},
        "users": {"eden": switchsaves.eden_users(_path(cfg["eden"])) if cfg["eden"].strip() else [],
                  "ryujinx": switchsaves.ryujinx_users(_path(cfg["ryujinx"])) if cfg["ryujinx"].strip() else []},
    }
    db.close()
    return {"summary": summary, "games": rows_games, "saves": saves, "unidentified": [f.public() for f in games.unidentified],
            "_scan": games}


def verify_all(cfg: Dict[str, Any], progress: Callable[[int, int, str], None], cancel: Any) -> Dict[str, int]:
    """Check the checksums of every game file in the games folder (each NCA against its own name). Files already checked and
    unchanged are skipped. Returns ``{checked, ok, damaged, skipped}``. Raises ``InterruptedError`` when cancelled."""
    folder = _path(cfg["games"])
    if folder is None or not folder.is_dir():
        raise FileNotFoundError("Choose the games folder first.")
    cache = switchverify.VerifyCache()
    out = {"checked": 0, "ok": 0, "damaged": 0, "skipped": 0}
    try:
        files = [Path(dp) / n for dp, _d, names in os.walk(folder) for n in sorted(names)
                 if n.lower().endswith((".nsp", ".xci")) and not n.startswith(".")]
        todo = []
        for path in files:
            got = cache.get(path)
            if got is not None:
                out["skipped"] += 1
            else:
                todo.append(path)
        total = sum(p.stat().st_size for p in todo)
        base = 0
        for path in todo:
            size = path.stat().st_size
            verdict = switchverify.verify_file(path, None, lambda done, _t, label, b=base: progress(b + done, total, label), cancel)
            cache.put(path, verdict)
            base += size
            if verdict.status == "ok":
                out["ok"] += 1
            elif verdict.status == "damaged":
                out["damaged"] += 1
            out["checked"] += 1
    finally:
        cache.close()
    return out


def rows(last: Optional[Dict[str, Any]], view: str, page: Callable[..., Dict[str, Any]], body: Dict[str, Any]) -> Dict[str, Any]:
    """One page of the games, the saves or the unidentified files of the last scan."""
    if last is None:
        return {"total": 0, "offset": 0, "limit": 0, "items": []}
    items = last.get(view) or []
    if view == "saves" and body.get("only") in ("both", "differ", "eden", "ryujinx", "nogame"):
        only = body["only"]
        items = [s for s in items if (only == "both" and s["eden"] and s["ryujinx"])
                 or (only == "differ" and s["state"] in ("eden newer", "ryujinx newer"))
                 or (only == "eden" and s["eden"] and not s["ryujinx"]) or (only == "ryujinx" and s["ryujinx"] and not s["eden"])
                 or (only == "nogame" and not s["has_game"])]
    if view == "games" and body.get("only") in ("damaged", "unchecked", "nosaves"):
        only = body["only"]
        items = [g for g in items if (only == "damaged" and g["checksums"] == "damaged") or (only == "unchecked" and g["checksums"] != "ok")
                 or (only == "nosaves" and not (g["saves"]["eden"] or g["saves"]["ryujinx"]))]
    texts = [(i, " ".join(str(i.get(k, "")) for k in ("name", "title_id", "file", "state")).lower()) for i in items]
    return page(texts, body.get("offset"), body.get("limit"), body.get("q"))
