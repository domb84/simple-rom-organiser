"""Keep the TOSEC and No-Intro DATs up to date without any manual download step.

An :class:`UpdateManager` owns one background thread that (1) checks the newest TOSEC release
and the libretro No-Intro ETags, then (2) downloads whatever is newer. It is *not* a job of the
server's JobManager and never waits for or interrupts scans.

Concurrency rule
    Downloads and extraction only touch ``cache/`` and sibling temp dirs. The only moments the
    live DATs change are the atomic commits (TOSEC directory swap including ``release.json``,
    No-Intro single-file ``os.replace``), and they are done while holding :attr:`dat_lock`.
    A scan takes the same lock while it *loads* DATs, so it always sees a consistent set and
    keeps working on what it parsed. If a commit replaces DATs that existed before,
    ``status()["scan_stale"]`` becomes true until :meth:`clear_stale` is called (the server
    calls it when a scan has loaded its DATs).

Offline
    A failed check keeps the cached DATs and sets a quiet ``notice``. Only when nothing at all
    is cached is it an ``error`` (code ``offline``), and :meth:`ensure` raises ``UpdateError``.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import errno
import http.client
import json
import os
import socket
import ssl
import tempfile
import threading
import time
import urllib.error
from pathlib import Path
from typing import Any, Callable, Optional

from . import nointro as _nointro
from . import paths, platforms
from . import tosec as _tosec
from .nointro import NOINTRO_DATS, NoIntroError
from .tosec import Cancelled, ReleaseInfo, TosecError

CHECK_TIMEOUT = 10        # seconds, per network request while checking
POLL = 0.2                # seconds, ensure() wait granularity
Progress = Callable[[int, int, str], None]

_NETWORK_ERRORS = (urllib.error.URLError, TimeoutError, ConnectionError, socket.gaierror,
                   socket.timeout, ssl.SSLError, http.client.HTTPException)
# plain OSErrors that really are connectivity problems (everything else - ENOSPC, EACCES,
# EROFS, ENOENT ... - is a local failure and must be reported, not shown as "offline")
_NETWORK_ERRNOS = {errno.ENETUNREACH, errno.ENETDOWN, errno.EHOSTUNREACH, errno.EHOSTDOWN,
                   errno.ETIMEDOUT, errno.ECONNRESET, errno.ECONNREFUSED, errno.ECONNABORTED,
                   errno.EPIPE, errno.ENETRESET}


class UpdateError(Exception):
    """A DAT update could not provide what was needed. ``code``: offline | failed | cancelled."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message or code


class _Token:
    """Cancel token that is set when any of its parts is set."""

    def __init__(self, *parts: Any) -> None:
        self._parts = [p for p in parts if p is not None]

    def is_set(self) -> bool:
        return any(p.is_set() for p in self._parts)


def _is_network_error(exc: BaseException) -> bool:
    """True for connectivity problems (no HTTP answer at all)."""
    if isinstance(exc, urllib.error.HTTPError):
        return False
    if isinstance(exc, TosecError):
        cause = exc.__cause__
        return cause is not None and _is_network_error(cause)
    if isinstance(exc, NoIntroError):
        cause = exc.__cause__
        if cause is not None:
            return _is_network_error(cause)
        return "could not download" in str(exc) or "offline" in str(exc).lower()
    if isinstance(exc, _NETWORK_ERRORS):
        if isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, OSError) \
                and not isinstance(exc.reason, _NETWORK_ERRORS):
            return exc.reason.errno in _NETWORK_ERRNOS
        return True
    return isinstance(exc, OSError) and exc.errno in _NETWORK_ERRNOS


def _iso(ts: float) -> str:
    return _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).isoformat(timespec="seconds")


def _day(iso: Optional[str]) -> str:
    return iso[:10] if iso else ""


class UpdateManager:
    def __init__(self, tosec: Any = _tosec, nointro: Any = _nointro, enabled: bool = True,
                 clock: Callable[[], float] = time.time,
                 state_path: Optional[Path] = None) -> None:
        self.tosec = tosec
        self.nointro = nointro
        self.enabled = bool(enabled) and not paths.offline_forced()
        self.clock = clock
        self.state_path = Path(state_path) if state_path is not None else None
        self.dat_lock = threading.Lock()
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._cancel = threading.Event()
        self._started = False
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._state = "idle"
        self._offline = False
        self._notice: Optional[str] = None
        self._error: Optional[dict[str, str]] = None
        self._scan_stale = False
        self._progress = {"done": 0, "total": 0, "message": "", "source": ""}
        self._last_checked: Optional[str] = None
        self._tosec_latest: Optional[str] = None
        self._tosec_checked: Optional[str] = None
        self._tosec_error = False
        self._nointro_checked: Optional[str] = None
        self._nointro_rows: dict[str, dict[str, Any]] = {}
        self._updating: Optional[str] = None   # source being downloaded right now
        self._load_state()

    # ------------------------------------------------------------------ persistence

    def _state_file(self) -> Path:
        return self.state_path if self.state_path is not None else paths.updates_path()

    def _load_state(self) -> None:
        try:
            data = json.loads(self._state_file().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        tos = data.get("tosec") if isinstance(data.get("tosec"), dict) else {}
        noi = data.get("nointro") if isinstance(data.get("nointro"), dict) else {}
        self._last_checked = data.get("checked_at") if isinstance(data.get("checked_at"), str) else None
        self._tosec_latest = tos.get("latest") if isinstance(tos.get("latest"), str) else None
        self._tosec_checked = tos.get("checked_at") if isinstance(tos.get("checked_at"), str) else None
        self._nointro_checked = noi.get("checked_at") if isinstance(noi.get("checked_at"), str) else None

    def _save_state(self) -> None:
        data = {"checked_at": self._last_checked,
                "tosec": {"latest": self._tosec_latest, "checked_at": self._tosec_checked},
                "nointro": {"checked_at": self._nointro_checked}}
        target = self._state_file()
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".updates-", suffix=".tmp", dir=str(target.parent))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(data, fh, indent=2)
                os.replace(tmp, target)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except OSError:
            pass  # remembering the check time is a convenience only

    # ------------------------------------------------------------------ status

    def _installed_nointro(self) -> tuple[Optional[str], dict[str, Optional[str]]]:
        try:
            local = {d.name: d.version or None for d in self.nointro.list_dats()}
        except Exception:  # noqa: BLE001 - status must never fail
            local = {}
        versions = [v for n, v in local.items() if n in NOINTRO_DATS and v]
        return (max(versions) if versions else None), local

    def _installed_tosec(self) -> Optional[str]:
        try:
            return self.tosec.installed_release()
        except Exception:  # noqa: BLE001
            return None

    def status(self) -> dict[str, Any]:
        """Thread-safe JSON snapshot (shape documented in docs/ARCHITECTURE.md, amendment 6)."""
        noi_installed, local = self._installed_nointro()
        tos_installed = self._installed_tosec()
        with self._lock:
            tos_status = self._tosec_status(tos_installed)
            rows = []
            for name in NOINTRO_DATS:
                row = self._nointro_rows.get(name, {})
                st = row.get("status")
                if self._updating == "nointro" and st in ("update_available", "unknown", "missing"):
                    st = "updating"
                if not st:
                    st = "unknown" if name in local else "absent"
                rows.append({"name": name, "version": local.get(name), "status": st})
            statuses = [r["status"] for r in rows]
            if self._updating == "nointro":
                noi_status = "updating"
            elif any(s == "error" for s in statuses):
                noi_status = "error"
            elif noi_installed is None:
                noi_status = "absent"
            elif any(s in ("update_available", "missing") for s in statuses):
                noi_status = "update_available"
            elif all(s == "up_to_date" for s in statuses):
                noi_status = "up_to_date"
            else:
                noi_status = "unknown"
            return {
                "enabled": self.enabled,
                "state": self._state,
                "running": self._running,
                "offline": self._offline,
                "notice": self._notice,
                "last_checked": self._last_checked,
                "scan_stale": self._scan_stale,
                "progress": dict(self._progress),
                "error": dict(self._error) if self._error else None,
                "tosec": {"installed": tos_installed, "latest": self._tosec_latest,
                          "status": tos_status, "checked_at": self._tosec_checked},
                "nointro": {"installed": noi_installed, "latest": None, "status": noi_status,
                            "dats": rows, "checked_at": self._nointro_checked},
            }

    def _tosec_status(self, installed: Optional[str]) -> str:
        if self._updating == "tosec":
            return "updating"
        if self._tosec_error:
            return "error"
        if installed is None:
            return "absent"
        if self._tosec_latest is None:
            return "unknown"
        return "up_to_date" if installed == self._tosec_latest else "update_available"

    def clear_stale(self) -> None:
        """A scan has (re)loaded the DATs: they are current again."""
        with self._lock:
            self._scan_stale = False

    # ------------------------------------------------------------------ control

    def start_background(self) -> None:
        """Server startup: check + download what is newer on a daemon thread (idempotent)."""
        with self._lock:
            if self._started or not self.enabled:
                return
            self._started = True
        self._spawn()

    def check(self, force: bool = False) -> bool:
        """The "Check for updates" button. False if disabled or an update is already running."""
        if not self.enabled:
            return False
        return self._spawn(force=force)

    def cancel(self) -> bool:
        with self._lock:
            if not self._running:
                return False
            self._cancel.set()
            return True

    def _spawn(self, force: bool = False) -> bool:
        with self._lock:
            if self._running:
                return False
            self._running = True
            self._begin()
        t = threading.Thread(target=self._run, kwargs={"force": force, "claimed": True},
                             name="romorg-updates", daemon=True)
        self._thread = t
        t.start()
        return True

    def _begin(self) -> None:
        """(lock held) reset the transient state for a new run."""
        self._cancel.clear()
        self._state = "checking"
        self._error = None
        self._updating = None
        self._progress = {"done": 0, "total": 0, "message": "Checking for updates", "source": ""}

    # ------------------------------------------------------------------ the run

    def _set_progress(self, done: int, total: int, message: str, source: str,
                      forward: Optional[Progress]) -> None:
        with self._lock:
            self._progress = {"done": done, "total": total, "message": message, "source": source}
        if forward is not None:
            try:
                forward(done, total, message)
            except Exception:  # noqa: BLE001 - a broken observer must not abort the update
                pass

    def _run(self, force: bool = False, scope: Optional[tuple[str, tuple[str, ...]]] = None,
             cancel: Any = None, forward: Optional[Progress] = None, claimed: bool = False) -> None:
        """Check, download, commit. ``claimed``: the caller already set ``_running``."""
        if not claimed:
            with self._lock:
                self._running = True
                self._begin()
        token = _Token(self._cancel, cancel)
        try:
            lock = getattr(self.tosec, "update_lock", None)
            with (lock(directory=paths.dats_dir().parent) if callable(lock) else contextlib.nullcontext()):
                self._do(force, scope, token, forward)
        except _tosec.Busy as exc:
            with self._lock:
                if self._has_cache(scope):
                    self._notice = str(exc)
                    self._error = None
                else:
                    self._error = {"code": "failed", "message": str(exc)}
        except Cancelled:
            with self._lock:
                self._notice = "Update cancelled"
                self._error = None
        except BaseException as exc:  # noqa: BLE001 - the thread must end cleanly
            with self._lock:
                if _is_network_error(exc) and self._has_cache(scope):
                    self._set_offline()
                else:
                    self._error = {"code": "offline" if _is_network_error(exc) else "failed",
                                   "message": str(exc) or exc.__class__.__name__}
        finally:
            with self._cond:
                self._running = False
                self._updating = None
                self._state = "error" if self._error else "idle"
                self._cancel.clear()
                self._progress = {"done": 0, "total": 0, "message": "", "source": ""}
                self._cond.notify_all()

    def _has_cache(self, scope: Optional[tuple[str, tuple[str, ...]]]) -> bool:
        if scope is not None:
            src, names = scope
            if src == "tosec":
                return self._installed_tosec() is not None
            return any(d.name in names for d in self.nointro.list_dats())
        return self._installed_tosec() is not None or bool(self.nointro.list_dats())

    def _set_offline(self) -> None:
        """(lock held) quiet notice when DATs are cached, an error when nothing is."""
        self._offline = True
        if self._has_cache(None):
            when = _day(self._last_checked)
            self._notice = "Offline - using the installed DATs" + (f" (checked {when})" if when else "")
            self._error = None
        else:
            self._notice = None
            self._error = {"code": "offline",
                           "message": "No DATs are installed and the update servers can not be "
                                      "reached. Check your connection and retry."}

    def _do(self, force: bool, scope: Optional[tuple[str, tuple[str, ...]]], token: _Token,
            forward: Optional[Progress]) -> None:
        want_tosec = scope is None or scope[0] == "tosec"
        want_nointro = scope is None or scope[0] == "nointro"
        names = (list(scope[1]) if scope is not None else list(NOINTRO_DATS)) if want_nointro else []

        # ---- check phase (small requests only)
        self._set_progress(0, 0, "Checking for updates", "", forward)
        info: Optional[ReleaseInfo] = None
        tosec_exc: Optional[BaseException] = None
        rows: list[dict[str, Any]] = []
        if want_nointro:
            rows = list(self.nointro.check_updates(names=names, timeout=CHECK_TIMEOUT))
        if token.is_set():
            raise Cancelled()
        if want_tosec:
            try:
                info = self.tosec.check_latest(timeout=CHECK_TIMEOUT)
            except Cancelled:
                raise
            except Exception as exc:  # noqa: BLE001 - classified below
                tosec_exc = exc
        if token.is_set():
            raise Cancelled()

        nointro_net = bool(rows) and all(r.get("status") == "error" for r in rows) and all(
            "HTTP" not in str(r.get("error", "")) for r in rows)
        tosec_net = tosec_exc is not None and _is_network_error(tosec_exc)
        checked_ok = (info is not None) or any(r.get("status") != "error" for r in rows)
        failed_all = (not want_tosec or tosec_exc is not None) and \
                     (not want_nointro or all(r.get("status") == "error" for r in rows))
        with self._lock:
            for r in rows:
                self._nointro_rows[r["name"]] = dict(r)
            self._tosec_error = False
            now = _iso(self.clock())
            if info is not None:
                self._tosec_latest = info.date
                self._tosec_checked = now
            elif tosec_exc is not None and not tosec_net:
                self._tosec_error = True
            if checked_ok:
                self._last_checked = now
                if want_nointro and any(r.get("status") != "error" for r in rows):
                    self._nointro_checked = now
                self._offline = False
                self._notice = None
                self._save_state()
        if failed_all and (tosec_net or not want_tosec) and (nointro_net or not want_nointro):
            with self._lock:
                self._set_offline()
            return
        if failed_all:
            msgs = [str(tosec_exc)] if tosec_exc is not None else []
            msgs += [str(r.get("error")) for r in rows[:1]]
            raise UpdateError("failed", "; ".join(m for m in msgs if m) or "Update check failed")

        errors: list[str] = []
        if tosec_exc is not None and not tosec_net:
            errors.append(f"TOSEC: {tosec_exc}")

        # ---- download + commit phase (No-Intro first: tiny, and usually what a scan waits for)
        todo = [r["name"] for r in rows if force or r.get("status") in ("update_available", "unknown", "missing")]
        if todo:
            before = {d.name for d in self.nointro.list_dats()}
            with self._lock:
                self._state = "downloading"
                self._updating = "nointro"
            try:
                res = self.nointro.update_dats(
                    names=todo, cancel=token, force=force, commit_lock=self.dat_lock,
                    progress=lambda d, t, m: self._set_progress(d, t, m, "nointro", forward))
            except NoIntroError as exc:
                if _is_network_error(exc) and self._has_cache(scope):
                    with self._lock:
                        self._set_offline()
                else:
                    errors.append(f"No-Intro: {exc}")
                res = None
            finally:
                with self._lock:
                    self._updating = None
            if res is not None:
                with self._lock:
                    for row in res.get("dats", []):
                        old = self._nointro_rows.setdefault(row["name"], {"name": row["name"]})
                        if row.get("status") in ("downloaded", "unchanged"):
                            old["status"] = "up_to_date"
                        elif row.get("status") == "error":
                            old["status"] = "error"
                            old["error"] = row.get("error")
                        if row.get("status") == "downloaded" and row["name"] in before:
                            self._scan_stale = True
                    if res.get("failed"):
                        errors.extend(r.get("error", "") for r in res["dats"]
                                      if r.get("status") == "error" and r.get("error"))
        if token.is_set():
            raise Cancelled()

        if info is not None:
            installed = self._installed_tosec()
            if force or installed != info.date:
                with self._lock:
                    self._state = "downloading"
                    self._updating = "tosec"
                try:
                    self.tosec.update_dats(
                        progress=lambda d, t, m: self._tosec_progress(d, t, m, forward),
                        cancel=token, force=force, info=info, commit_lock=self.dat_lock)
                except Cancelled:
                    raise
                except Exception as exc:  # noqa: BLE001
                    if _is_network_error(exc) and self._has_cache(scope):
                        with self._lock:
                            self._set_offline()
                    else:
                        with self._lock:
                            self._tosec_error = True
                        errors.append(f"TOSEC: {exc}")
                else:
                    with self._lock:
                        if installed is not None:
                            self._scan_stale = True
                finally:
                    with self._lock:
                        self._updating = None
        if errors:
            raise UpdateError("failed", "; ".join(errors))

    def _tosec_progress(self, done: int, total: int, message: str, forward: Optional[Progress]) -> None:
        with self._lock:
            if message.lower().startswith(("extract", "install")):
                self._state = "installing"
        self._set_progress(done, total, message, "tosec", forward)

    # ------------------------------------------------------------------ ensure

    @staticmethod
    def _present(platform: platforms.Platform) -> bool:
        return bool(platforms.locate_dats(platform))

    def ensure(self, platform: platforms.Platform, progress: Optional[Progress] = None,
               cancel: Any = None) -> None:
        """Return when at least one DAT of ``platform`` is installed, updating first if needed.

        Waits for a running update (forwarding its progress) or runs one here (single-flight).
        Raises :class:`UpdateError` (``offline`` | ``failed`` | ``cancelled``).
        """
        if self._present(platform):
            return
        if not self.enabled:
            raise UpdateError("offline", "The DATs for this system are not installed and "
                                         "automatic updates are disabled.")
        scope = (platforms.source_of(platform), tuple(platform.dats))
        last = None
        while True:
            if cancel is not None and cancel.is_set():
                self.cancel()
                raise UpdateError("cancelled", "Cancelled while updating the DATs")
            claimed = False
            with self._lock:
                if not self._running:
                    self._running = True
                    self._begin()
                    claimed = True
            if claimed:
                self._run(scope=scope, cancel=cancel, forward=progress, claimed=True)
                break
            if self._present(platform):
                return
            snap = self.status()["progress"]
            if progress is not None and snap != last and snap.get("message"):
                last = snap
                try:
                    progress(snap["done"], snap["total"], snap["message"])
                except Exception:  # noqa: BLE001
                    pass
            with self._cond:
                if self._running:
                    self._cond.wait(POLL)
            # not running any more and still nothing installed: loop and run our own scoped update
        if self._present(platform):
            return
        st = self.status()
        if cancel is not None and cancel.is_set():
            raise UpdateError("cancelled", "Cancelled while updating the DATs")
        if st["error"]:
            raise UpdateError(st["error"]["code"], st["error"]["message"])
        if st["offline"]:
            raise UpdateError("offline", "The DATs for this system are not installed and the "
                                         "update servers can not be reached.")
        raise UpdateError("failed", "The DATs for this system could not be installed.")
