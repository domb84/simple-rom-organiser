"""Real-browser checks of the UI (headless Chromium / Brave over the DevTools protocol).

Skipped unless a browser is available: set ROMORG_BROWSER_PORT to an already running ``--remote-debugging-port``, or have
Chromium / Chrome on the PATH, or (Steam Deck) the Brave flatpak. The app runs for real on a small synthetic Amiga library
(``tests/uifixture.py``); the checks drive the UI the three ways it is used (a mocked Gamepad API, keys, clicks) and fail on any
JavaScript error."""
from __future__ import annotations

import os
import hashlib
import json
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
import zlib
from pathlib import Path
from unittest import mock
os.environ.setdefault("ROMORG_RETROARCH_DETECT", "0")      # never pick up a RetroArch installed on this machine

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from cdp import Browser  # noqa: E402
from uifixture import GBA_DAT, Fixture  # noqa: E402

_browser: Browser | None = None
_proc: subprocess.Popen | None = None
_profile = ""            # the browser's throw-away profile folder (removed when the module is done)
_unavailable = ""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _flags(port: int, profile: str) -> list[str]:
    return ["--headless=new", "--no-sandbox", "--disable-gpu", f"--remote-debugging-port={port}",
            f"--user-data-dir={profile}", "--no-first-run",
            # the test browser stays offline: no component downloads (they also leave folders in the temp folder)
            "--disable-component-update", "--disable-background-networking", "about:blank"]


def setUpModule() -> None:
    global _browser, _proc, _profile, _unavailable
    if os.environ.get("ROMORG_NO_BROWSER"):
        _unavailable = "ROMORG_NO_BROWSER is set"
        return
    port = os.environ.get("ROMORG_BROWSER_PORT")
    if port:
        _browser = Browser.connect(int(port))
        return
    port_n = _free_port()
    profile = _profile = tempfile.mkdtemp(prefix="romorg-browser-")
    cmd = None
    for exe in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable", "brave-browser"):
        if shutil.which(exe):
            cmd = [exe] + _flags(port_n, profile)
            break
    if cmd is None and shutil.which("flatpak-spawn"):
        probe = subprocess.run(["flatpak-spawn", "--host", "flatpak", "info", "com.brave.Browser"],
                               capture_output=True, timeout=30)
        if probe.returncode == 0:
            cmd = ["flatpak-spawn", "--host", "flatpak", "run", "com.brave.Browser"] + _flags(port_n, profile)
    if cmd is None:
        _unavailable = "no Chromium / Chrome / Brave found (set ROMORG_BROWSER_PORT to use a running one)"
        return
    _proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            _browser = Browser.connect(port_n)
            return
        except OSError:
            time.sleep(0.3)
    _unavailable = "the browser did not start"


def tearDownModule() -> None:
    if _proc is not None:
        if _browser is not None:
            _browser.shutdown()            # quits every process of it, also when it runs on the host (flatpak-spawn)
        _proc.terminate()
        try:
            _proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _proc.kill()
            _proc.wait()
    if _profile:
        # every run made a new profile (30 to 40 MB) and none was ever removed. The browser's helper processes let
        # go of their files a moment after the main one exits.
        for _ in range(20):
            shutil.rmtree(_profile, ignore_errors=True)
            if not os.path.exists(_profile):
                break
            time.sleep(0.25)


# Runs in the page before its own script: answers some API calls itself (``rules``) and notes every call in
# ``window.__calls``, so a test can show the page a slow or a failing server, and see what it asked for.
_FETCH_STUB = r"""(() => {
  const real = window.fetch.bind(window);
  const rules = %s;
  window.__calls = [];
  window.fetch = async (url, opts = {}) => {
    const u = new URL(url, location.href), method = (opts.method || 'GET').toUpperCase();
    let sent = null;
    try { sent = opts.body ? JSON.parse(opts.body) : null; } catch (_) { sent = null; }
    window.__calls.push({ method, path: u.pathname, body: sent });
    const json = (status, data) => new Response(JSON.stringify(data), { status, headers: { 'Content-Type': 'application/json' } });
    for (const r of rules) {
      if (r.path !== u.pathname || (r.method || 'GET') !== method) continue;
      if (r.delay) { await new Promise((res) => setTimeout(res, r.delay)); if (!r.patch && r.body === undefined) return real(url, opts); }
      if (r.patch) { const res = await real(url, opts); return json(res.status, Object.assign(await res.json(), r.patch)); }
      return json(r.status || 200, r.body);
    }
    return real(url, opts);
  };
})()"""


class UiTestCase(unittest.TestCase):
    PAD = """(() => {
      const pad = { id: 'mock', index: 0, connected: true, mapping: 'standard', axes: [0, 0, 0, 0],
                    buttons: Array.from({length: 17}, () => ({pressed: false, value: 0})) };
      window.__pad = pad;
      navigator.getGamepads = () => [pad, null, null, null];
      window.__tap = (i) => new Promise((done) => { pad.buttons[i].pressed = true;
        setTimeout(() => { pad.buttons[i].pressed = false; setTimeout(done, 90); }, 90); });
      window.dispatchEvent(new Event('gamepadconnected'));
    })()"""
    BUTTON = {"A": 0, "B": 1, "X": 2, "Y": 3, "LB": 4, "RB": 5, "LT": 6, "RT": 7, "View": 8, "Menu": 9,
              "up": 12, "down": 13, "left": 14, "right": 15}

    def setUp(self) -> None:
        if _browser is None:
            self.skipTest(_unavailable or "no browser")
        self.fx = Fixture()
        self.addCleanup(self.fx.close)
        self.page = _browser.new_page()
        self.addCleanup(self.page.close)
        self.page.call("Emulation.setFocusEmulationEnabled", enabled=True)       # (a headless page is not "focused": no :focus without it)
        self.page.call("Emulation.setDeviceMetricsOverride", width=1280, height=720, deviceScaleFactor=1, mobile=False)

    # -- helpers
    def stub(self, *rules: dict) -> None:
        """Before the page loads: answer these calls in the page (see ``_FETCH_STUB``)."""
        self.page.call("Page.addScriptToEvaluateOnNewDocument", source=_FETCH_STUB % json.dumps(list(rules)))

    def api(self, method: str, path: str, body: object = None):
        req = urllib.request.Request(self.fx.url.rstrip("/") + path, method=method, data=json.dumps(body or {}).encode() if method == "POST" else None,
                                     headers={"X-Romorg-Token": "t", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())

    def set_archive(self, folder: str, platform: str = "") -> None:
        """Choose the archive folder (there is no default one), the way Settings does; the page must be open."""
        body = {"dir": folder, **({"platform": platform} if platform else {})}
        self.page.eval("""(async()=>{const t=document.querySelector('meta[name=romorg-token]').content;
 const r = await fetch('/api/settings/archive',{method:'POST',headers:{'Content-Type':'application/json','X-Romorg-Token':t},body:JSON.stringify(%s)});
 return r.status})()""" % json.dumps(body))

    def scan(self, platform: str = "Commodore Amiga", root: str = "") -> None:
        js = """(async()=>{const t=document.querySelector('meta[name=romorg-token]').content;
 await fetch('/api/scan',{method:'POST',headers:{'Content-Type':'application/json','X-Romorg-Token':t},
   body:JSON.stringify({path:%r,platform:%r})});
 for(let i=0;i<300;i++){const j=await fetch('/api/job').then(r=>r.json()); if(j&&j.status!=='running')return j.status;
   await new Promise(r=>setTimeout(r,100));} return 'timeout'})()""" % (root or str(self.fx.root), platform)
        self.assertEqual(self.page.eval(js), "done")

    def click(self, selector: str, text: str = "") -> None:
        self.page.wait(f"!!{self._find(selector, text)}")
        self.page.eval(f"{self._find(selector, text)}.click()")

    @staticmethod
    def _find(selector: str, text: str) -> str:
        if text:
            return f"[...document.querySelectorAll({selector!r})].find(e => e.textContent.trim().startsWith({text!r}))"
        return f"document.querySelector({selector!r})"

    def start(self, hash_: str = "#/", scanned: bool = True) -> None:
        self.page.goto(self.fx.url)
        if scanned:
            self.scan()
        self.page.goto(self.fx.url + hash_)
        self.page.wait("!!window.RomorgNext && document.querySelector('#main').children.length > 0", timeout=30)
        self.page.eval(self.PAD)

    def pad(self) -> None:
        """Use the controller from now on (its first press would otherwise only show the focus)."""
        self.page.eval("RomorgNext.setMode('pad')")

    def go(self, hash_: str, ready: str) -> None:
        self.page.eval(f"location.hash = {hash_!r}")
        self.page.wait(ready, timeout=60)

    def tap(self, name: str) -> None:
        self.page.eval(f"window.__tap({self.BUTTON[name]})")

    def key(self, key: str, **mods: bool) -> None:
        self.page.call("Input.dispatchKeyEvent", type="keyDown", key=key, code=mods.pop("code", ""), windowsVirtualKeyCode=mods.pop("vk", 0), **mods)
        self.page.call("Input.dispatchKeyEvent", type="keyUp", key=key)

    def active(self) -> str:
        return self.page.eval("(() => { const e = document.activeElement; return e ? (e.id || e.dataset.key || e.getAttribute('href') || e.textContent.trim().slice(0, 40)) : ''; })()")

    def strip(self) -> str:
        return self.page.eval("document.getElementById('strip').textContent")

    def main(self) -> str:
        return self.page.eval("document.getElementById('main').textContent")

    def rows(self) -> list[str]:
        return self.page.eval("[...document.querySelectorAll('#main [data-section=list] .row .row-name')].map(e => e.firstChild ? e.firstChild.textContent : '')")

    def list_ready(self, count: int = 1) -> None:
        self.page.wait(f"document.querySelectorAll('#main [data-section=list] .row').length >= {count}", timeout=60)

    def search(self, text: str) -> None:
        """The Files screen's search (Y), its answer listed."""
        self.tap("Y")
        self.page.eval(f"(() => {{ const q = document.getElementById('files-q'); q.value = {text!r}; q.dispatchEvent(new Event('input')); }})()")
        self.page.wait("document.getElementById('main').textContent.includes('found')", timeout=30)

    def retroarch(self, cfg_text: str) -> tuple[Path, Path]:
        """A RetroArch of its own (a folder with retroarch.cfg) chosen as the one the app uses: ``(its folder, a work folder)``."""
        base = Path(tempfile.mkdtemp(prefix="romorg-ui-ra-"))
        self.addCleanup(shutil.rmtree, base, True)
        ra = base / "ra"
        ra.mkdir()
        (ra / "retroarch.cfg").write_text(cfg_text.replace("{base}", str(base)))
        self.page.goto(self.fx.url)
        self.assertIn("installs", self.api("POST", "/api/retroarch/select", {"custom": str(ra), "backup_dir": str(base / "backups")}))
        return ra, base

    def no_js_errors(self) -> None:
        self.assertEqual(self.page.errors, [])


class EveryRuleOn(UiTestCase):
    """The fixture's counts are those of every rule on: bad dumps excluded too."""

    def setUp(self) -> None:
        super().setUp()
        from romorg import library
        self.api("POST", "/api/library/defaults", {"exclude": sorted(library.RULES)})


class NoMachineFolders:
    """Mixin: no emulator is installed for the test (what this machine has must not be found and selected)."""

    def setUp(self) -> None:
        super().setUp()
        home = Path(tempfile.mkdtemp(prefix="romorg-ui-home-"))
        self.addCleanup(shutil.rmtree, home, True)
        env = mock.patch.dict(os.environ, {"HOME": str(home)})
        env.start()
        self.addCleanup(env.stop)


class StartupTests(UiTestCase):
    def test_the_page_asks_for_status_and_platforms_once(self) -> None:
        self.start(scanned=False)
        self.page.wait("!!document.getElementById('home-archive')")
        names = self.page.eval("performance.getEntriesByType('resource').map(e => new URL(e.name).pathname)")
        self.assertEqual(names.count("/api/status"), 1)
        self.assertEqual(names.count("/api/platforms"), 1)
        self.no_js_errors()


class NextUiTests(EveryRuleOn):
    """The screens by controller (a mocked Gamepad API), keyboard and mouse."""

    def test_home_lists_the_jobs_and_the_systems_and_a_controller_brings_the_focus_and_the_strip(self) -> None:
        self.start()
        self.page.wait("!!document.getElementById('home-archive')")
        self.assertFalse(self.page.eval("!!document.getElementById('home-build')"))   # (no Build tile: a build is a system's)
        self.assertEqual(self.page.eval("getComputedStyle(document.getElementById('strip')).display"), "none")     # the mouse: no strip
        self.assertIn("78%", self.page.eval("document.querySelector('[data-key=commodore-amiga]')?.textContent"))   # (7 of 9 games)
        self.tap("down")                                       # the first press only shows where the focus is
        self.page.wait("document.body.classList.contains('pad')")
        self.assertEqual(self.active(), "commodore-amiga")              # the system used last
        self.assertIn("Find a system", self.strip())
        self.tap("Y")                                          # Y: the search box
        self.assertEqual(self.active(), "home-find")
        self.page.eval("(() => { const f = document.getElementById('home-find'); f.value = 'lynx'; f.dispatchEvent(new Event('input')); })()")
        visible = "[...document.querySelectorAll('#home-systems .tile')].filter(t => !t.classList.contains('hidden')).map(t => t.dataset.key)"
        self.assertEqual(self.page.eval(visible), ["atari-lynx"])
        self.no_js_errors()

    def test_a_system_shows_the_tiles_of_the_setting_and_the_size_of_the_next_build(self) -> None:
        self.start("#/system/commodore-amiga")
        self.page.wait("document.querySelector('#sys-build .tile-sub')?.textContent === '10 files will change'", timeout=60)
        self.page.wait("!!document.getElementById('sys-tiles') && !document.querySelector('#sys-tiles .loading')", timeout=60)
        keys = self.page.eval("[...document.querySelectorAll('#sys-tiles > *')].map(e => e.dataset.key || e.id)")
        self.assertEqual(keys, ["have_all", "have_rules", "sys-build", "needs_look", "files_to_change"])  # unmatched, BIOS, saves: none
        self.assertIn("7of 9", self.page.eval("document.querySelector('[data-key=have_all]')?.textContent"))   # (games: 10 of 12 disks)
        # Missing: what the rules keep that is not here (as Files › Missing), not every game of the database
        t = self.api("GET", "/api/library/totals?platform=Commodore%20Amiga")
        games = t["missing_games"]                                                       # (system disks are never missing)
        want = f"{games} game{'s' if games != 1 else ''}" if games else ""
        missing = "[...document.querySelectorAll('#main a.tile')].find(a => a.textContent.startsWith('Missing'))"
        if want:
            self.page.wait(f"{missing}?.querySelector('.tile-sub')?.textContent === {want!r} && !{missing}.classList.contains('hidden')", timeout=60)
        else:
            self.page.wait(f"{missing}?.classList.contains('hidden')", timeout=60)
        self.tap("down")
        self.assertEqual(self.active(), "sys-build")
        self.tap("B")                                          # B: home
        self.page.wait("!!document.getElementById('home-archive')")
        self.no_js_errors()

    def test_the_tiles_setting_changes_what_a_system_shows(self) -> None:
        self.start("#/settings/tiles")
        self.page.wait("document.querySelectorAll('#main [data-key]').length > 5")
        self.page.eval("document.querySelector('#main [data-key=have_all]').click()")
        self.page.wait("document.querySelector('#main [data-key=have_all]')?.getAttribute('aria-checked') === 'false'")
        self.go("#/system/commodore-amiga", "!!document.getElementById('sys-tiles')")
        self.assertFalse(self.page.eval("!!document.querySelector('#sys-tiles [data-key=have_all]')"))
        self.no_js_errors()

    def test_build_library_by_controller_moves_the_files(self) -> None:
        arch = Path(tempfile.mkdtemp(prefix="romorg-ui-arch-"))                  # a build needs an archive folder (outside the ROM folder)
        self.addCleanup(shutil.rmtree, arch, True)
        self.start("#/")
        self.set_archive(str(arch))
        self.go("#/system/commodore-amiga/build", "!!document.getElementById('build-start')")
        self.page.wait("!!document.getElementById('build-start')", timeout=60)
        self.assertIn("10 files will change", self.page.eval("document.getElementById('main').textContent"))
        self.tap("down")                                       # the focus is on Start build
        self.assertEqual(self.active(), "build-start")
        self.tap("A")
        self.page.wait("document.getElementById('main').textContent.includes('Library built')", timeout=120)
        self.assertTrue((self.fx.root / "Commodore Amiga - Games").is_dir())                                  # (a folder for each game)
        done = self.page.eval("document.getElementById('main').textContent")
        self.assertIn("6 files moved or renamed", done)                                                        # (the other 4 of the 10 go to the archive)
        self.assertIn("4 files moved to the archive", done)
        self.assertTrue(any(d.is_dir() for d in (arch / self.fx.root.name).iterdir()))
        self.no_js_errors()

    def test_the_keyboard_moves_goes_back_and_names_its_keys(self) -> None:
        self.start("#/settings")
        self.page.wait("!!document.getElementById('set-archive')")
        self.key("ArrowDown", code="ArrowDown", vk=40)
        self.page.wait("document.body.classList.contains('keys')")
        self.assertEqual(self.active(), "set-rules")
        self.key("ArrowRight", code="ArrowRight", vk=39)
        self.assertEqual(self.active(), "set-archive")
        self.assertIn("Enter", self.strip())
        self.assertIn("Esc", self.strip())
        self.key("Escape", code="Escape", vk=27)
        self.page.wait("!!document.getElementById('home-archive')")
        self.no_js_errors()

    def test_the_folder_browser_goes_up_and_picks_a_folder(self) -> None:
        self.start("#/system/commodore-amiga/folder")
        self.page.wait("document.querySelector('.crumb-path')?.textContent.endsWith('amiga')")
        self.tap("B")                                          # B: up a folder
        self.page.wait(f"document.querySelector('.crumb-path')?.textContent === {str(self.fx.tmp)!r}")
        self.tap("Y")                                          # Y: use this folder
        self.page.wait("!!document.getElementById('sys-tiles') || !!document.getElementById('sys-scan')", timeout=30)
        folders = self.page.eval("fetch('/api/status').then(r => r.json()).then(s => s.folders['Commodore Amiga'])")
        self.assertEqual(folders, str(self.fx.tmp))
        self.no_js_errors()

    def test_s3_needs_its_fields_before_it_saves(self) -> None:
        self.start("#/settings/archive/s3", scanned=False)
        self.page.wait("!!document.getElementById('s3-save')")
        self.page.eval("document.getElementById('s3-save').click()")
        self.assertIn("Endpoint, access key and bucket are needed", self.page.eval("document.querySelector('.panel')?.textContent"))
        self.page.eval("document.getElementById('s3-service').click()")
        self.page.wait("!!document.querySelector('.sheet')")
        self.assertIn("Choose", self.strip())
        self.tap("down"); self.tap("up"); self.tap("A")        # MEGA S4
        self.page.wait("!document.querySelector('.sheet')")
        self.assertEqual(self.page.eval("document.getElementById('s3-endpoint').value"), "s3.g.megas4.com")
        self.no_js_errors()

    def test_files_opens_folders_and_a_game_can_be_always_kept(self) -> None:
        self.start("#/system/commodore-amiga/files?view=excluded")
        self.page.wait("document.querySelectorAll('#main [data-section=list] .row').length >= 1", timeout=60)
        self.assertTrue(self.page.eval("document.querySelector('.chip.on')?.textContent").startswith("Excluded"))
        self.click("#main [data-section=list] .row", "_excluded")
        self.page.wait("document.querySelector('.crumb-path')?.textContent === '_excluded'", timeout=30)
        self.assertTrue(any("Delta Force" in r for r in self.rows()), self.rows())
        self.click("#main [data-section=list] .row", "Delta Force")
        self.page.wait("!!document.querySelector('.sheet')")
        self.pad()
        self.tap("A")                                          # the first choice: Always keep
        self.page.wait("!document.querySelector('.sheet')")
        self.page.wait("[...document.querySelectorAll('.toast')].some(t => t.textContent.includes('Always kept'))", timeout=30)
        prof = self.api("GET", "/api/library/profile?platform=Commodore%20Amiga")
        self.assertIn("Delta Force", str(prof["profile"]["overrides"]))
        self.tap("B")                                          # up a folder
        self.page.wait("document.querySelector('.crumb-path')?.textContent === 'amiga'", timeout=30)
        self.no_js_errors()

    def test_files_search_finds_games_in_every_folder(self) -> None:
        self.start("#/system/commodore-amiga/files")
        self.page.wait("document.querySelectorAll('#main [data-section=list] .row').length >= 1", timeout=60)
        self.tap("down")
        self.tap("Y")
        self.assertEqual(self.active(), "files-q")
        self.page.eval("(() => { const q = document.getElementById('files-q'); q.value = 'epsilon'; q.dispatchEvent(new Event('input')); })()")
        self.page.wait("document.getElementById('main').textContent.includes('found')", timeout=30)
        names = self.rows()
        self.assertTrue(names and all("Epsilon" in n for n in names), names)
        self.no_js_errors()

    def test_files_filters_narrow_the_view(self) -> None:
        self.start("#/system/commodore-amiga/files?view=excluded")
        self.page.wait("document.querySelectorAll('#main [data-section=list] .row').length >= 1", timeout=60)
        self.tap("down")
        self.tap("X")
        self.page.wait("!!document.querySelector('.sheet')")
        self.click(".sheet .row", "Language")
        self.page.wait("document.querySelectorAll('.sheet').length === 2")
        self.click(".sheet:last-of-type .row", "De")
        self.page.wait("document.querySelectorAll('.sheet').length === 0 && !!document.querySelector('.views .tag')", timeout=30)
        self.page.wait("document.querySelector('.chip.on')?.textContent === 'Excluded 1'", timeout=30)
        self.no_js_errors()

    def test_archived_files_are_ticked_and_restored(self) -> None:
        arch = Path(tempfile.mkdtemp(prefix="romorg-ui-arch-"))
        self.addCleanup(shutil.rmtree, arch, True)
        mine = arch / self.fx.root.name / "_unmatched" / "notes"
        mine.mkdir(parents=True)
        (mine / "readme.txt").write_bytes(b"my notes")
        self.page.goto(self.fx.url)
        self.set_archive(str(arch))
        self.start("#/system/commodore-amiga/files?view=archive")
        self.page.wait("document.querySelectorAll('#main [data-section=list] .row').length >= 1", timeout=60)
        self.click("#main [data-section=list] .row", "_unmatched")
        self.page.wait("document.querySelector('.crumb-path')?.textContent === '_unmatched'", timeout=30)
        self.click("#main [data-section=list] .row", "notes")
        self.page.wait("document.querySelector('.crumb-path')?.textContent === '_unmatched/notes'", timeout=30)
        self.tap("down")
        self.tap("Y")                                          # tick it
        self.page.wait("document.querySelector('.panel-title')?.textContent === '1 ticked'")
        self.page.eval("RomorgNext.press('Start')")
        self.page.wait("[...document.querySelectorAll('.toast')].some(t => t.textContent.includes('Restored 1'))", timeout=60)
        self.assertEqual((self.fx.root / "notes" / "readme.txt").read_bytes(), b"my notes")
        self.assertTrue((mine / "readme.txt").is_file())       # the archive keeps its copy
        self.no_js_errors()

    def test_a_rule_set_for_one_system_goes_back_to_every_system(self) -> None:
        self.start("#/system/commodore-amiga/rules")
        self.page.wait("!!document.querySelector('#main [data-key=bad_dump]')", timeout=30)
        tag = "document.querySelector('#main [data-key=bad_dump] .tag')?.textContent"
        self.assertEqual(self.page.eval(tag), "Every system")
        self.page.eval("document.querySelector('#main [data-key=bad_dump]').click()")
        self.page.wait(f"{tag} === 'This system'", timeout=30)
        self.assertNotIn("bad_dump", self.api("GET", "/api/library/profile?platform=Commodore%20Amiga")["profile"]["exclude"])
        self.tap("down")
        self.page.eval("document.querySelector('#main [data-key=bad_dump]').focus()")
        self.tap("X")
        self.page.wait(f"{tag} === 'Every system'", timeout=30)
        self.assertIn("bad_dump", self.api("GET", "/api/library/profile?platform=Commodore%20Amiga")["profile"]["exclude"])
        self.no_js_errors()

    def test_the_region_order_is_changed_by_picking_a_row_up(self) -> None:
        self.start("#/settings/rules", scanned=False)
        self.page.wait("!!document.querySelector('#main [data-key=region_priority]')", timeout=30)
        before = self.api("GET", "/api/library/defaults")["profile"]["region_priority"]
        self.page.eval("document.querySelector('#main [data-key=region_priority]').click()")
        self.page.wait("!!document.querySelector('.sheet .order-row')")
        self.pad()
        self.tap("A")                                          # pick the first one up
        self.page.wait("!!document.querySelector('.sheet .row.lifted')")
        self.tap("down")
        self.tap("A")                                          # put it down
        self.page.wait("!document.querySelector('.sheet .row.lifted')")
        self.page.eval("RomorgNext.press('Start')")            # Done
        self.page.wait("!document.querySelector('.sheet')")
        self.page.wait(f"document.querySelector('#main [data-key=region_priority] .row-value')?.textContent.startsWith({before[1]!r})", timeout=30)
        after = self.api("GET", "/api/library/defaults")["profile"]["region_priority"]
        self.assertEqual(after[:2], [before[1], before[0]])
        self.no_js_errors()

    def test_disc_images_has_the_two_checks_and_keeps_chdman(self) -> None:
        self.start("#/settings/disc", scanned=False)
        self.page.wait("!!document.querySelector('#main [data-key=dvd]')", timeout=30)
        self.assertTrue(self.page.eval("!!document.querySelector('#main [data-key=chdman]') && !!document.querySelector('#main [data-key=engine]')"))
        self.page.eval("document.querySelector('#main [data-key=dvd]').click()")
        self.page.wait("document.querySelector('#main [data-key=dvd]')?.getAttribute('aria-checked') === 'true'")
        info = self.api("GET", "/api/chdman")
        self.assertEqual((info["verify_dvd"], info["verify_audio"]), (True, False))
        self.no_js_errors()

    def test_needs_a_look_leaves_out_what_the_rules_exclude_on_purpose(self) -> None:
        self.start("#/system/commodore-amiga/look")
        self.page.wait("document.querySelectorAll('#main [data-section=list] .row').length >= 1", timeout=60)
        text = self.page.eval("document.getElementById('main').textContent")
        self.assertNotIn("Eta Escape", text)                  # (excluded by the language rule: a choice, not a problem)
        self.assertIn("Nothing to look at here", text)
        self.assertEqual(self.page.eval("document.querySelectorAll('.views .chip').length"), 0)        # (one list: no incomplete sets, the build archives them)
        self.go("#/system/commodore-amiga", "!!document.querySelector('[data-key=needs_look]') && !document.querySelector('#sys-tiles .loading')")
        self.assertIn("0games", self.page.eval("document.querySelector('[data-key=needs_look]')?.textContent.replace(/\\s/g, '')"))
        self.no_js_errors()

    def test_your_library_says_what_the_rules_keep_and_what_you_lack(self) -> None:
        self.start("#/system/commodore-amiga")
        self.page.wait("!!document.querySelector('a[data-key=have_rules]')", timeout=60)
        self.click("a[data-key=have_rules]")
        self.page.wait("document.getElementById('main').textContent.includes('with your rules')", timeout=60)
        text = self.page.eval("document.getElementById('main').textContent")
        self.assertIn("7 games with your rules", text)
        self.assertIn("You have 5; 2 missing.", text)
        self.assertIn("Theta Tower", text)                    # the games you lack, by name
        self.no_js_errors()

    def test_the_library_count_stays_the_library_count_on_the_archive_view(self) -> None:
        self.start("#/system/commodore-amiga/files")
        self.page.wait("document.querySelector('.chip.on')?.textContent === 'Library 5'", timeout=60)
        self.click(".views .chip", "Archive")
        self.page.wait("document.querySelector('.chip.on')?.textContent.startsWith('Archive')", timeout=60)
        self.assertEqual(self.page.eval("[...document.querySelectorAll('.views .chip')][0].textContent"), "Library 5")
        self.no_js_errors()

    def test_a_games_folder_says_what_kind_of_game_it_is_and_shows_its_playlist(self) -> None:
        self.start("#/system/commodore-amiga/files")
        self.page.wait("document.querySelectorAll('#main [data-section=list] .row').length >= 1", timeout=60)
        self.click("#main [data-section=list] .row", "Commodore Amiga - Games")
        self.page.wait("document.querySelectorAll('#main [data-section=list] .row .row-tags .tag').length >= 4", timeout=30)
        chips = self.page.eval("[...document.querySelector('#main [data-section=list] .row .row-tags').children].map(t => t.textContent)")
        self.assertEqual(chips[:2], ["OCS", "En"])                                   # (machine, language [, version]; "WHDLoad" when it is played from one)
        self.page.eval("[...document.querySelectorAll('#main [data-section=list] .row')].find(e => e.textContent.startsWith('Epsilon')).focus()")
        self.page.wait("document.querySelector('#main .panel').textContent.includes('Machine')", timeout=30)         # (the panel says it in words)
        panel = self.page.eval("document.querySelector('#main .panel').textContent")
        for word in ("OCS", "The disks, through a playlist", "floppy disks", "English"):
            self.assertIn(word, panel)
        self.click("#main [data-section=list] .row", "Epsilon")
        self.page.wait("[...document.querySelectorAll('#main [data-section=list] .row')].some(r => r.textContent.includes('.m3u') && r.textContent.includes('Playlist'))", timeout=30)
        self.no_js_errors()

    def test_the_filters_are_on_the_files_page_itself(self) -> None:
        self.start("#/system/commodore-amiga/files")
        self.page.wait("document.querySelectorAll('.filter-bar .chip').length >= 2", timeout=60)
        names = self.page.eval("[...document.querySelectorAll('.filter-bar .chip')].map(c => c.dataset.filter)")
        self.assertIn("machine", names)
        self.assertIn("language", names)
        self.click(".filter-bar .chip", "Platform")                                                  # (the choices open from the chip, no sheet first)
        self.page.wait("!!document.querySelector('.overlay, [role=dialog]')", timeout=30)
        self.no_js_errors()

    def test_upgrade_and_whdload_are_switches_not_choices(self) -> None:
        from romorg import server
        with mock.patch.object(server.App, "_upgrade_names", return_value={"x"}), mock.patch.object(server.App, "_has_upgrade", return_value=True):
            self.start("#/system/commodore-amiga/files")
            self.page.wait("!!document.querySelector('.filter-bar [data-filter=upgrade]')", timeout=60)
            pressed = "document.querySelector('.filter-bar [data-filter=upgrade]')?.getAttribute('aria-pressed')"
            self.assertEqual(self.page.eval(pressed), "false")
            self.click(".filter-bar .chip", "Upgrade available")
            self.page.wait(f"{pressed} === 'true'", timeout=30)                                    # (one press: on, no picker)
            self.assertFalse(self.page.eval("!!document.querySelector('.overlay, [role=dialog]')"))
            self.click(".filter-bar .chip", "Upgrade available")
            self.page.wait(f"{pressed} === 'false'", timeout=30)
        self.no_js_errors()

    def test_the_side_panel_stays_in_view_down_a_long_folder(self) -> None:
        self.start("#/system/commodore-amiga/files?view=library")
        self.page.wait("document.querySelectorAll('#main [data-section=list] .row').length >= 1", timeout=60)
        self.click("#main [data-section=list] .row", "Commodore Amiga - Games")
        self.page.wait("document.querySelectorAll('#main [data-section=list] .row').length >= 5", timeout=30)
        self.pad()
        self.page.eval("[...document.querySelectorAll('#main [data-section=list] .row')].pop().focus()")
        self.page.wait("document.getElementById('main').scrollTop > 0")                # the list scrolled to the last row
        box = self.page.eval("""(() => { const p = document.querySelector('#main .panel').getBoundingClientRect(),
            m = document.getElementById('main').getBoundingClientRect(); return [p.top >= m.top - 1, p.bottom <= m.bottom + 1,
            document.querySelector('#main .panel .panel-title')?.textContent || ''] })()""")
        self.assertEqual(box[:2], [True, True])                                       # pinned: still on screen
        self.assertTrue(box[2])                                                      # and it describes the focused row
        self.no_js_errors()

    def test_a_task_carries_on_when_its_screen_is_left_and_says_when_it_is_done(self) -> None:
        self.start("#/system/commodore-amiga")
        self.page.wait("!!document.getElementById('sys-build')", timeout=60)
        self.pad()
        self.page.eval("""(() => { const real = window.fetch; window.fetch = async (u, o) => {        // (a scan that takes a moment)
            if (String(u).includes('/api/job') && !window.__slowDone) { window.__slowDone = true; await new Promise(r => setTimeout(r, 400)); }
            return real(u, o); }; })()""")
        self.tap("X")                                          # rescan
        self.page.wait("document.getElementById('main').textContent.includes('carries on')", timeout=30)
        self.tap("B")                                          # leave it: it goes on
        self.page.wait("location.hash === '#/'", timeout=30)
        self.page.wait("[...document.querySelectorAll('.toast')].some(t => t.textContent.includes('Scan finished'))", timeout=60)
        self.no_js_errors()

    def test_tiles_shimmer_until_their_numbers_are_ready_and_the_chip_says_why(self) -> None:
        self.start("#/system/commodore-amiga")
        self.page.wait("!!document.getElementById('sys-tiles')", timeout=60)
        seen = self.page.eval("""new Promise(done => { const t0 = Date.now(); const look = () => {
            const loading = document.querySelector('#sys-tiles .loading .bone');
            const chip = !document.getElementById('head-activity').classList.contains('hidden');
            if (loading || chip || Date.now() - t0 > 5000) done([!!loading, chip]); else setTimeout(look, 20); }; look(); })""")
        self.assertTrue(any(seen), seen)                       # something said it was working (a shimmering tile or the chip)
        self.page.wait("!document.querySelector('#sys-tiles .loading')", timeout=60)
        self.page.wait("document.getElementById('topline').classList.contains('hidden')", timeout=60)
        self.no_js_errors()

    def test_asking_first_starts_on_the_safe_button(self) -> None:
        arch = Path(tempfile.mkdtemp(prefix="romorg-ui-arch-"))
        self.addCleanup(shutil.rmtree, arch, True)
        self.page.goto(self.fx.url)
        self.set_archive(str(arch))
        self.start("#/settings/archive", scanned=False)
        self.click("#main .row", "Stop archiving")
        self.page.wait("!!document.getElementById('confirm-no')")
        self.page.eval("RomorgNext.setMode('pad')")
        self.assertEqual(self.active(), "confirm-no")
        self.tap("A")
        self.page.wait("!document.getElementById('confirm-no')")
        self.assertEqual(self.api("GET", "/api/status")["archive"]["dir"], str(arch))
        self.no_js_errors()

    def test_retroarch_saves_are_moved_by_controller(self) -> None:
        ra, base = self.retroarch('savefile_directory = "{base}/old"\nsavestate_directory = "{base}/old"\n'
                                  'sort_savefiles_enable = "true"\nsort_savestates_enable = "true"\n')
        (base / "old" / "bsnes").mkdir(parents=True)
        (base / "old" / "bsnes" / "Mario (USA).srm").write_bytes(b"s")
        (base / "old" / "bsnes" / "Mario (USA).state1").write_bytes(b"t")
        (base / "new" / "saves").mkdir(parents=True)
        self.start("#/settings/retroarch", scanned=False)
        self.page.wait("!!document.getElementById('ra-saves')", timeout=30)
        self.click("#ra-saves")
        self.page.wait("!!document.getElementById('ra-save-dir')", timeout=30)
        self.click("#ra-save-dir")                             # the folder browser, at the folder used now
        self.page.wait(f"document.querySelector('.crumb-path')?.textContent === {str(base / 'old')!r}", timeout=30)
        self.page.eval("RomorgNext.press('B')")                # up a folder
        self.page.wait(f"document.querySelector('.crumb-path')?.textContent === {str(base)!r}", timeout=30)
        self.click("#main [data-section=list] .row", "new")
        self.page.wait(f"document.querySelector('.crumb-path')?.textContent === {str(base / 'new')!r}", timeout=30)
        self.click("#main [data-section=list] .row", "saves")
        self.page.wait(f"document.querySelector('.crumb-path')?.textContent === {str(base / 'new' / 'saves')!r}", timeout=30)
        self.page.eval("RomorgNext.press('Y')")                # use it
        self.page.wait(f"document.querySelector('#ra-save-dir .row-value')?.textContent.startsWith({str(base / 'new' / 'saves')!r})", timeout=30)
        self.page.eval("RomorgNext.press('Start')")            # preview
        self.page.wait("document.getElementById('main').textContent.includes('files will move')", timeout=60)
        self.assertIn("savefile_directory", self.page.eval("document.getElementById('main').textContent"))
        self.assertTrue((base / "old" / "bsnes" / "Mario (USA).srm").is_file())            # nothing moved by the preview
        self.page.eval("RomorgNext.press('Start')")            # move
        self.page.wait("document.getElementById('head-title').textContent === 'Saves moved'", timeout=60)
        self.assertTrue((base / "new" / "saves" / "bsnes" / "Mario (USA).srm").is_file())
        self.assertTrue((base / "new" / "saves" / "bsnes" / "Mario (USA).state1").is_file())
        self.assertIn(str(base / "new" / "saves"), (ra / "retroarch.cfg").read_text())
        self.assertTrue(list((base / "backups").glob("saves-*.zip")))                   # the backup zip, in the folder chosen
        self.no_js_errors()

    def test_retroarch_points_at_one_shared_folder(self) -> None:
        ra, base = self.retroarch('playlist_directory = "{base}/shared/playlists"\n')
        (base / "shared" / "playlists").mkdir(parents=True)
        (base / "shared" / "thumbnails").mkdir(parents=True)
        self.start("#/settings/retroarch/assets", scanned=False)
        self.page.wait("document.querySelectorAll('#main .row').length >= 5", timeout=30)
        self.pad()
        self.tap("X")                                          # one shared folder for all
        self.page.wait("!!document.getElementById('ra-shared-go')", timeout=30)
        self.assertEqual(self.page.eval("document.querySelector('#main [data-key=thumbnails_directory]')?.getAttribute('aria-checked')"), "true")
        self.assertIn("Already used", self.page.eval("document.getElementById('main').textContent"))   # (playlists)
        self.page.eval("RomorgNext.press('Start')")
        self.page.wait("location.hash === '#/settings/retroarch/assets'", timeout=30)
        self.assertIn(str(base / "shared" / "thumbnails"), (ra / "retroarch.cfg").read_text())
        self.no_js_errors()

    def test_a_systems_databases_with_their_counts(self) -> None:
        self.start("#/system/commodore-amiga/databases")
        self.page.wait("document.querySelectorAll('#main .row').length >= 1", timeout=30)
        text = self.page.eval("document.getElementById('main').textContent")
        self.assertIn("Commodore Amiga - Games - [ADF]", text)
        self.assertIn("10 of 12", text)
        self.assertIn("Installed", text)
        self.no_js_errors()

    def test_the_menu_opens_and_b_closes_it(self) -> None:
        self.start(scanned=False)
        self.page.wait("!!document.getElementById('home-archive')")
        self.page.eval("document.getElementById('head-menu').click()")
        self.page.wait("!!document.querySelector('.sheet')")
        self.tap("B")
        self.page.wait("!document.querySelector('.sheet')")
        self.no_js_errors()


class ControllerTests(UiTestCase):
    """The controller, on a mocked Gamepad API (a pad whose buttons the test presses and releases), and Steam Input presenting it as keys."""

    def test_the_d_pad_walks_the_screen_and_a_presses_and_b_goes_back(self) -> None:
        self.start(scanned=False)
        self.page.wait("!!document.getElementById('home-archive')")
        self.pad()
        self.page.eval("document.getElementById('home-find').focus()")
        seen = {self.active()}
        for _ in range(5):
            self.tap("down")
            seen.add(self.active())
        self.assertGreaterEqual(len(seen), 4, seen)                                      # each press moved to another control
        self.assertNotIn("", seen)
        self.page.eval("document.getElementById('home-settings').focus()")
        self.tap("A")
        self.page.wait("location.hash === '#/settings' && !!document.getElementById('set-rules')")
        self.tap("B")
        self.page.wait("!!document.getElementById('home-archive')")
        self.no_js_errors()

    def test_a_move_stays_in_its_part_of_the_screen_until_nothing_lies_that_way(self) -> None:
        self.start("#/settings/archive/s3", scanned=False)
        self.page.wait("!!document.getElementById('s3-save')")
        self.pad()
        self.page.eval("document.getElementById('s3-service').focus()")
        part = "document.activeElement.closest('[data-section]')?.dataset.section"
        for _ in range(6):                                                               # down the fields: never into the panel at their side
            self.tap("down")
            self.assertEqual(self.page.eval(part), "fields", self.active())
        self.tap("right")                                                                # nothing to the right in the fields: the panel
        self.assertEqual(self.page.eval(part), "panel")
        self.tap("left")                                                                 # and back
        self.assertEqual(self.page.eval(part), "fields")
        self.no_js_errors()

    def test_focus_stays_in_a_panel_and_comes_back_when_it_closes(self) -> None:
        self.start("#/settings/archive/s3", scanned=False)
        self.page.wait("!!document.getElementById('s3-save')")
        self.pad()
        self.page.eval("document.getElementById('s3-service').focus()")
        self.tap("A")                                                                    # opens the list picker
        self.page.wait("!!document.querySelector('.sheet')")
        inside = "document.querySelector('.sheet').contains(document.activeElement)"
        self.page.wait(inside)
        for name in ("down", "down", "right", "up", "left", "down"):
            self.tap(name)
            self.assertTrue(self.page.eval(inside), name)                                # the D-pad never leaves the panel
        self.assertIn("Cancel", self.strip())
        self.tap("B")
        self.page.wait("!document.querySelector('.sheet')")
        self.page.wait("document.activeElement?.id === 's3-service'", timeout=10)        # back where it was
        self.no_js_errors()

    def test_the_bumpers_switch_views_and_y_finds_the_search_box(self) -> None:
        self.start("#/system/commodore-amiga/files")
        chip = "document.querySelector('.views .chip.on')?.dataset.view"
        self.page.wait(f"{chip} === 'library'", timeout=60)
        self.list_ready()
        self.pad()
        self.tap("RB")
        self.page.wait(f"{chip} === 'excluded'", timeout=30)
        self.tap("LB")
        self.page.wait(f"{chip} === 'library'", timeout=30)
        self.list_ready()
        self.tap("Y")
        self.assertEqual(self.active(), "files-q")
        self.assertIn("Keyboard", self.strip())                                          # (Steam's keyboard types in it)
        self.no_js_errors()

    def test_the_bumpers_go_to_the_next_system(self) -> None:
        self.page.goto(self.fx.url)
        self.api("POST", "/api/folders", {"platform": "Nintendo Game Boy Advance", "path": str(self.fx.gba)})
        self.start("#/system/commodore-amiga")
        self.page.wait("!!document.getElementById('sys-build')", timeout=60)
        self.tap("RB")
        self.page.wait("location.hash === '#/system/nintendo-game-boy-advance'", timeout=30)
        self.tap("LB")
        self.page.wait("location.hash === '#/system/commodore-amiga'", timeout=30)
        self.no_js_errors()

    def test_menu_opens_the_app_menu_and_b_closes_it(self) -> None:
        self.start(scanned=False)
        self.page.wait("!!document.getElementById('home-archive')")
        self.pad()
        self.tap("Menu")
        self.page.wait("document.querySelector('.sheet')?.contains(document.activeElement)")
        text = self.page.eval("document.querySelector('.sheet').textContent")
        self.assertIn("Quit", text)
        self.assertNotIn("Classic", text)
        self.tap("down")
        self.assertTrue(self.page.eval("document.querySelector('.sheet').contains(document.activeElement)"))
        self.tap("B")
        self.page.wait("!document.querySelector('.sheet')")
        self.no_js_errors()

    def test_the_triggers_scroll_the_screen(self) -> None:
        self.start("#/settings/rules", scanned=False)
        self.page.wait("!!document.querySelector('#main [data-key=region_priority]')", timeout=30)
        self.pad()
        self.tap("RT")
        self.page.wait("document.getElementById('main').scrollTop > 100", timeout=10)
        self.tap("LT")
        self.page.wait("document.getElementById('main').scrollTop < 50", timeout=10)
        self.no_js_errors()

    def test_arrow_keys_do_the_same_where_steam_input_acts_as_a_keyboard(self) -> None:
        self.page.goto(self.fx.url + "?deck=1")
        self.page.wait("!!window.RomorgNext && !!document.getElementById('home-archive')", timeout=30)
        self.assertTrue(self.page.eval("document.body.classList.contains('deck')"))
        self.assertNotEqual(self.page.eval("getComputedStyle(document.getElementById('strip')).display"), "none")   # (the Deck layout: always on)
        self.page.eval("RomorgNext.setMode('keys')")
        self.page.eval("document.getElementById('home-find').focus()")
        self.key("ArrowRight", code="ArrowRight", vk=39)                                 # in a line of text left / right are the caret's ...
        self.assertEqual(self.active(), "home-find")
        self.key("ArrowDown", code="ArrowDown", vk=40)
        self.assertNotEqual(self.active(), "home-find")                                  # ... but down leaves it
        seen = {self.active()}
        for _ in range(3):
            self.key("ArrowDown", code="ArrowDown", vk=40)
            seen.add(self.active())
        self.assertGreaterEqual(len(seen), 3, seen)
        self.no_js_errors()

    def test_a_server_started_for_the_deck_tells_the_page(self) -> None:
        self.fx.server.app.deck = True
        self.start(scanned=False)
        self.page.wait("!!document.getElementById('home-archive')")
        self.assertTrue(self.page.eval("document.body.classList.contains('deck')"))
        self.page.eval("RomorgNext.setMode('keys')")
        self.page.wait("document.getElementById('strip').textContent.includes('Open')")
        self.assertNotIn("Enter", self.strip())                                          # the strip names the Deck's buttons, not keys
        self.no_js_errors()

    def test_the_page_is_unchanged_without_a_controller_or_the_deck_flag(self) -> None:
        self.start(scanned=False)
        self.page.wait("!!document.getElementById('home-archive')")
        self.assertFalse(self.page.eval("document.body.classList.contains('deck') || document.body.classList.contains('pad')"))
        self.assertEqual(self.page.eval("getComputedStyle(document.getElementById('strip')).display"), "none")
        self.no_js_errors()


class RulesUiTests(UiTestCase):
    """Rules for every system (Settings) and a system's own (its Rules screen): a rule follows every system's until it is set for one."""

    ROW = "document.querySelector('#main [data-key=latest_only]')"

    def test_a_rule_set_for_every_system_reaches_the_systems_that_follow_it(self) -> None:
        self.start("#/settings/rules", scanned=False)
        self.page.wait(f"!!{self.ROW}", timeout=30)
        want = self.page.eval(f"{self.ROW}.getAttribute('aria-checked') !== 'true'")
        self.page.eval(f"{self.ROW}.click()")
        self.page.wait(f"{self.ROW}?.getAttribute('aria-checked') === {str(want).lower()!r}", timeout=30)
        self.assertEqual(self.api("GET", "/api/library/defaults")["profile"]["latest_only"], want)
        self.go("#/system/commodore-amiga/rules", f"!!{self.ROW}?.querySelector('.tag')")
        self.assertEqual(self.page.eval(f"{self.ROW}.querySelector('.tag').textContent"), "Every system")
        self.assertEqual(self.page.eval(f"{self.ROW}.getAttribute('aria-checked') === 'true'"), want)
        self.assertEqual(self.page.eval("document.getElementById('head-aside').textContent"), "All follow every system")
        self.no_js_errors()

    def test_a_rule_only_some_systems_have_is_only_on_those_systems(self) -> None:
        row = "document.querySelector('#main [data-key=prefer_unprotected]')"
        self.start("#/settings/rules", scanned=False)
        self.page.wait(f"!!{row}", timeout=30)                                           # the Amiga's rule: a default too
        self.go("#/system/commodore-amiga/rules", f"!!{row}?.querySelector('.tag')")
        self.go("#/system/sony-playstation/rules", "!!document.querySelector('#main [data-key=latest_only]')?.querySelector('.tag') && "
                                                    "document.getElementById('head-crumbs').textContent.includes('PlayStation')")
        self.assertFalse(self.page.eval(f"!!{row}"))                                     # (a disc system has no such rule)
        self.no_js_errors()

    def test_the_system_follows_a_rule_change_by_itself(self) -> None:
        self.start("#/system/commodore-amiga")
        self.page.wait("document.querySelector('#sys-build .tile-sub')?.textContent.includes('will change')", timeout=60)
        before = self.api("POST", "/api/library/plan", {"limit": 1})["plan_id"]
        want = not self.api("GET", "/api/library/profile?platform=Commodore%20Amiga")["profile"]["latest_only"]
        self.go("#/system/commodore-amiga/rules", f"!!{self.ROW}")
        self.page.eval(f"{self.ROW}.click()")
        self.page.wait(f"{self.ROW}?.querySelector('.tag')?.textContent === 'This system'", timeout=30)
        self.assertEqual(self.page.eval("document.getElementById('head-aside').textContent"), "All follow every system")   # (drawn before the change)
        self.assertEqual(self.api("GET", "/api/library/profile?platform=Commodore%20Amiga")["profile"]["latest_only"], want)
        self.go("#/system/commodore-amiga", "document.querySelector('#sys-build .tile-sub')?.textContent.includes('will change')")
        plan = self.api("POST", "/api/library/plan", {"limit": 1})
        self.assertEqual(plan["profile"]["latest_only"], want)
        self.assertNotEqual(plan["plan_id"], before)
        self.no_js_errors()


class BiosUiTests(UiTestCase):
    def test_the_screen_says_where_the_checksums_come_from(self) -> None:
        self.start("#/settings/bios", scanned=False)
        self.page.wait("document.getElementById('main').textContent.includes('Where the checksums come from')", timeout=30)
        self.assertTrue(self.page.eval("document.getElementById('bios-check').disabled"))           # (no RetroArch was found)
        self.assertIn("No RetroArch was found", self.main())
        self.no_js_errors()

    def test_the_check_finds_a_file_and_places_it(self) -> None:
        ra, base = self.retroarch('system_directory = "{base}/ra/system"\n')
        (ra / "cores").mkdir()
        (ra / "cores" / "x_libretro.info").write_text('display_name = "Commodore - Amiga (X)"\ncorename = "X"\nsupported_extensions = "adf"\n'
                                                      'firmware_count = 1\nfirmware0_path = "kick34005.A500"\nfirmware0_opt = "false"\n')
        (self.fx.root / "kick34005.A500").write_bytes(b"kick")
        self.api("POST", "/api/folders", {"platform": "Commodore Amiga", "path": str(self.fx.root)})
        self.start("#/settings/bios", scanned=False)
        self.click("#bios-check")
        self.click("#main .panel .btn.primary", "Copy found files into place")
        self.click("#confirm-yes")
        self.page.wait("[...document.querySelectorAll('.toast')].some(t => t.textContent.includes('1 placed'))", timeout=30)
        self.assertEqual((ra / "system" / "kick34005.A500").read_bytes(), b"kick")
        self.no_js_errors()

    def test_the_tile_counts_the_bios_files_found_among_the_games(self) -> None:
        data = b"a firmware image, as far as the checksums go" * 20
        (self.fx.data / "dats" / "Atari Lynx - Firmware (TOSEC-v2012-09-08_CM).dat").write_text(
            '<?xml version="1.0"?><datafile><header><name>Atari Lynx - Firmware</name><version>2012-09-08</version></header>'
            f'<game name="Boot ROM"><description>x</description><rom name="boot.rom" size="{len(data)}" crc="{zlib.crc32(data):08x}" '
            f'md5="{hashlib.md5(data).hexdigest()}" sha1="{hashlib.sha1(data).hexdigest()}"/></game></datafile>')
        (self.fx.root / "somewhere.adf").write_bytes(data)
        self.start("#/system/commodore-amiga")
        self.page.wait("!!document.querySelector('#sys-tiles [data-key=bios]')", timeout=30)
        self.assertIn("1", self.page.eval("document.querySelector('#sys-tiles [data-key=bios] .tile-number').textContent"))
        self.no_js_errors()


class DatabasesUiTests(UiTestCase):
    def test_every_database_is_listed_with_its_version(self) -> None:
        self.start("#/settings/databases", scanned=False)
        self.list_ready(12)
        rows = self.page.eval("""[...document.querySelectorAll('#main [data-section=list] .row')].map(r => ({
            name: r.querySelector('.row-name').firstChild.textContent, sub: r.querySelector('small')?.textContent || '', tag: r.querySelector('.tag')?.textContent || ''}))""")
        names = {r["name"]: r for r in rows}
        for wanted in ("Sega - Dreamcast", "Sony - PlayStation 2", "Nintendo - Wii", "Nintendo - Game Boy Advance",
                       "Nintendo Switch: games, every update version, add-ons", "Nintendo Wii U: the disc games, with region and languages"):
            self.assertIn(wanted, names)
        self.assertIn("20250101-000000", names["Nintendo - Game Boy Advance"]["sub"])    # the fixture's DAT version
        self.assertEqual(names["Sega - Dreamcast"]["tag"], "Not installed")
        self.assertIn("Nintendo Wii U", names["Nintendo Wii U: the disc games, with region and languages"]["sub"])
        self.assertTrue(self.page.eval("!!document.getElementById('db-check')"))
        self.no_js_errors()


class FileDetailTests(EveryRuleOn):
    def test_a_game_opens_into_what_is_known_about_it_with_its_checksums(self) -> None:
        self.start("#/system/commodore-amiga/files?view=excluded")
        self.list_ready()
        self.click("#main [data-section=list] .row", "_excluded")
        self.page.wait("document.querySelector('.crumb-path')?.textContent === '_excluded'", timeout=30)
        self.page.eval("[...document.querySelectorAll('#main [data-section=list] .row')].find(e => e.textContent.includes('Delta Force')).focus()")
        self.page.wait("document.querySelector('#main .panel').textContent.includes('Why')", timeout=30)
        self.assertRegex(self.page.eval("document.querySelector('#main .panel').textContent"), r"(?i)excluded")
        self.click("#main [data-section=list] .row", "Delta Force")
        self.page.wait("!!document.querySelector('.sheet')")
        self.click(".sheet .row", "More details")
        self.page.wait("[...document.querySelectorAll('.sheet .row')].some(r => r.textContent.includes('SHA-1'))", timeout=30)
        text = self.page.eval("document.querySelector('.sheet').textContent")
        self.assertIn("Commodore Amiga - Games - [ADF]", text)                           # the database it came from
        self.assertIn("CRC32", text)
        self.assertNotIn("null", text)
        self.no_js_errors()

    def test_a_game_you_lack_shows_no_stray_text(self) -> None:
        self.start("#/system/commodore-amiga/files?view=missing")
        self.list_ready()
        self.page.eval("document.querySelector('#main [data-section=list] button.row').focus()")
        self.page.wait("document.querySelector('#main .panel').textContent.includes('Not in your folder')", timeout=30)
        self.assertNotIn("null", self.page.eval("document.querySelector('#main .panel').textContent"))
        self.no_js_errors()

    def test_always_exclude_a_game_and_the_plan_follows(self) -> None:
        target = "Beta Blaster (1992)(Bits).adf"
        self.start("#/system/commodore-amiga/files")
        self.list_ready()
        self.pad()
        self.tap("down")
        self.search("Beta Blaster")
        self.click("#main [data-section=list] .row", "Beta Blaster")
        self.page.wait("!!document.querySelector('.sheet')")
        self.click(".sheet .row", "Always exclude")
        self.page.wait("[...document.querySelectorAll('.toast')].some(t => t.textContent.includes('Always excluded'))", timeout=30)
        row = [r for r in self.api("POST", "/api/library/plan", {"limit": 500})["items"] if r.get("from", "").endswith(target)][0]
        self.assertEqual(row["category"], "excluded")
        self.assertIn("always exclude", row["reason"].lower())
        self.no_js_errors()

    def test_needs_a_look_shows_the_better_version_you_lack(self) -> None:
        eu, us = b"europe rom " * 400, b"usa rom " * 400
        games = "".join(
            f'game (\n\tname "Solo Quest ({r})"\n\tdescription "Solo Quest ({r})"\n\trom ( name "Solo Quest ({r}).gba" size {len(d)} '
            f'crc {zlib.crc32(d):08x} md5 {hashlib.md5(d).hexdigest()} sha1 {hashlib.sha1(d).hexdigest()} )\n)\n'
            for r, d in (("Europe", eu), ("USA", us)))
        (self.fx.data / "nointro" / f"{GBA_DAT}.dat").write_text(
            f'clrmamepro (\n\tname "{GBA_DAT}"\n\tdescription "{GBA_DAT}"\n\tversion "20250101-000000"\n)\n' + games)
        (self.fx.gba / "Solo Quest (USA).gba").write_bytes(us)
        self.page.goto(self.fx.url)
        self.scan("Nintendo Game Boy Advance", str(self.fx.gba))
        for _ in range(100):
            if self.api("GET", "/api/library/upgrades?platform=Nintendo%20Game%20Boy%20Advance")["ready"]:
                break
            time.sleep(0.1)
        self.page.goto(self.fx.url + "#/system/nintendo-game-boy-advance/look")
        self.list_ready()
        self.click("#main [data-section=list] .row", "Solo Quest")
        self.page.wait("document.querySelector('#main .panel').textContent.includes('Solo Quest (Europe)')", timeout=30)
        self.assertIn("prefer a version you do not have", self.page.eval("document.querySelector('#main .panel').textContent"))
        self.no_js_errors()


class BuildElsewhereTests(UiTestCase):
    def test_build_in_another_folder_leaves_the_source_alone(self) -> None:
        dest = Path(tempfile.mkdtemp(prefix="romorg-ui-lib-"))
        self.addCleanup(shutil.rmtree, dest, True)
        before = sorted(str(p.relative_to(self.fx.root)) for p in self.fx.root.rglob("*"))
        self.api("POST", "/api/library/export/settings", {"enabled": False, "dest": str(dest), "mode": "copy"})
        self.start("#/system/commodore-amiga/settings")
        self.page.wait("!!document.querySelector('#main [data-key=where]')", timeout=30)
        self.click("#main [data-key=where]")
        self.click(".sheet .row", "In another folder")
        self.page.wait(f"document.querySelector('.crumb-path')?.textContent === {str(dest)!r}", timeout=30)
        self.page.eval("RomorgNext.press('Y')")                                          # use it
        self.page.wait("location.hash === '#/system/commodore-amiga/settings' && !!document.querySelector('#main [data-key=where]')", timeout=30)
        self.go("#/system/commodore-amiga/build", "!!document.getElementById('build-start')")
        self.assertIn("this folder is only read", self.main())
        self.click("#build-start")
        self.page.wait("document.getElementById('main').textContent.includes('Library built')", timeout=120)
        self.assertIn(f"files built in {dest}", self.main())
        built = [p for p in dest.rglob("*") if p.is_file() and ".romorg-library" not in p.parts]
        self.assertTrue(built)
        self.assertEqual(sorted(str(p.relative_to(self.fx.root)) for p in self.fx.root.rglob("*")), before)
        self.go("#/system/commodore-amiga/settings", "!!document.querySelector('#main [data-key=where]')")   # the choice is remembered
        self.assertIn("In another folder", self.page.eval("document.querySelector('#main [data-key=where] .row-value').textContent"))
        self.no_js_errors()


class ArchiveFolderTests(UiTestCase):
    """The archive folder is set once for every system (Settings › Archive); a system can have its own."""

    def test_a_system_can_have_an_archive_folder_of_its_own_and_a_build_uses_it(self) -> None:
        base = Path(tempfile.mkdtemp(prefix="romorg-ui-set-"))
        self.addCleanup(shutil.rmtree, base, True)
        (base / "everything").mkdir()
        (base / "amiga-only").mkdir()
        self.page.goto(self.fx.url)
        self.set_archive(str(base / "everything"))
        self.start("#/system/commodore-amiga/settings")
        self.page.wait("!!document.querySelector('#main a[href=\"#/settings/archive/folder?system=commodore-amiga\"]')", timeout=30)
        self.assertIn("Same as every system", self.main())
        self.click("#main a[href=\"#/settings/archive/folder?system=commodore-amiga\"]")
        self.page.wait(f"document.querySelector('.crumb-path')?.textContent === {str(base / 'everything')!r}", timeout=30)
        self.page.eval("RomorgNext.press('B')")                                          # up a folder
        self.page.wait(f"document.querySelector('.crumb-path')?.textContent === {str(base)!r}", timeout=30)
        self.click("#main [data-section=list] .row", "amiga-only")
        self.page.wait(f"document.querySelector('.crumb-path')?.textContent === {str(base / 'amiga-only')!r}", timeout=30)
        self.page.eval("RomorgNext.press('Y')")                                          # use it
        self.page.wait("document.getElementById('main').textContent.includes('Systems with their own archive')", timeout=30)
        self.assertIn(str(base / "amiga-only"), self.main())
        self.assertEqual(self.api("GET", "/api/status")["archive"]["dir"], str(base / "everything"))
        self.go("#/system/commodore-amiga/build", "!!document.getElementById('build-start')")
        self.assertIn(str(base / "amiga-only"), self.main())
        self.click("#build-start")
        self.page.wait("document.getElementById('main').textContent.includes('Library built')", timeout=120)
        self.assertTrue(any((base / "amiga-only").rglob("*.adf")))
        self.assertFalse(any((base / "everything").rglob("*.adf")))
        self.assertFalse([p for p in self.fx.root.glob("_*")])                           # (nothing stays in the ROM folder)
        self.no_js_errors()


class DiscImageUiTests(UiTestCase):
    """Settings › Disc images and the rule that converts raw discs to CHD first, with the built-in writer (a one-track synthetic PlayStation
    disc, no chdman, no audio so no libFLAC is needed)."""

    SLUG, PLATFORM = "sony-playstation", "Sony PlayStation"

    def setUp(self) -> None:
        super().setUp()
        import chdtestlib as T
        from romorg import paths
        patch = mock.patch.dict(os.environ, {"ROMORG_CHDMAN": os.path.join(self.fx.tmp, "no-such-chdman")})
        patch.start()
        self.addCleanup(patch.stop)
        track = T.make_data_track(12, 5)
        T.write_dat(paths.redump_dir() / "Sony - PlayStation.dat", [("Solo (USA)", "Games", [track])],
                    name="Sony - PlayStation")
        self.psx = self.fx.tmp / "psx"
        (self.psx / "raw").mkdir(parents=True)
        (self.psx / "raw" / "t.bin").write_bytes(track)
        (self.psx / "raw" / "t.cue").write_text('FILE "t.bin" BINARY\n  TRACK 01 MODE1/2352\n    INDEX 01 00:00:00\n')

    def test_the_compression_and_the_writer_are_saved(self) -> None:
        from romorg import chdwrite
        self.start("#/settings/disc", scanned=False)
        self.page.wait("!!document.querySelector('#main [data-key=preset]')", timeout=30)
        value = "document.querySelector('#main [data-key=preset] .row-value').textContent"
        self.assertIn("Standard", self.page.eval(value))
        self.click("#main [data-key=preset]")
        self.page.wait("!!document.querySelector('.sheet')")
        offered = self.page.eval("[...document.querySelectorAll('.sheet .row')].map(r => r.textContent)")
        self.assertEqual(any("Zstandard" in o for o in offered), chdwrite.zstd_available())
        if chdwrite.zstd_available():
            self.click(".sheet .row", "Zstandard")
            self.page.wait(f"{value}.includes('Zstandard')", timeout=30)
            self.assertEqual(self.api("GET", "/api/chdman")["preset"], "zstd")
            self.page.eval("location.reload()")                                          # saved on the server: survives a reload
            self.page.wait(f"{value}.includes('Zstandard')", timeout=30)
        else:
            self.page.eval("RomorgNext.press('B')")
        self.click("#main [data-key=writer]")
        self.click(".sheet .row", "chdman, when it is installed")
        self.page.wait("document.querySelector('#main [data-key=writer] .row-value').textContent.includes('chdman')", timeout=30)
        self.assertEqual(self.api("GET", "/api/chdman")["writer"], "chdman")
        self.no_js_errors()

    def test_the_library_converts_raw_discs_when_building(self) -> None:
        arch = Path(tempfile.mkdtemp(prefix="romorg-ui-arch-"))                           # a build needs an archive folder (outside the ROM folder)
        self.addCleanup(shutil.rmtree, arch, True)
        self.page.goto(self.fx.url)
        self.set_archive(str(arch))
        self.scan(self.PLATFORM, str(self.psx))
        self.start("#/settings/rules", scanned=False)
        row = "document.querySelector('#main [data-key=delete_converted]')"
        self.page.wait(f"{row}?.getAttribute('aria-checked') === 'true'", timeout=30)     # (both on by default)
        self.assertEqual(self.page.eval("document.querySelector('#main [data-key=convert_first]').getAttribute('aria-checked')"), "true")
        self.page.eval(f"{row}.click()")                                                  # off: the originals are kept
        self.page.wait(f"{row}?.getAttribute('aria-checked') === 'false'", timeout=30)
        profile = self.api("GET", "/api/library/defaults")["profile"]
        self.assertEqual((profile["convert_first"], profile["delete_converted"]), (True, False))
        self.go(f"#/system/{self.SLUG}/build", "!!document.getElementById('build-start')")
        self.assertIn("Converted first", self.main())
        self.click("#build-start")
        self.page.wait("document.getElementById('main').textContent.includes('Library built')", timeout=180)
        self.assertIn("1 converted first", self.main())
        self.assertTrue((self.psx / "Solo (USA)" / "Solo (USA).chd").is_file())
        self.assertTrue((self.psx / "_converted_originals" / "raw" / "t.bin").is_file())
        self.assertFalse((self.psx / "raw" / "t.bin").exists())
        self.no_js_errors()


class WorkInProgressTests(UiTestCase):
    """The line on top and the header chip: what the app is working on, wherever you are."""

    def run_job(self):
        """Start a scan-like job on the fixture's server whose progress the test sets step by step."""
        import threading
        app = self.fx.server.app
        gate, reported = threading.Event(), threading.Event()
        box = {"step": None}

        def work(job):
            while not gate.is_set():
                if box["step"] is not None:
                    job.report(*box["step"][:2], box["step"][2])
                    box["step"] = None
                    reported.set()
                gate.wait(0.02)

        self.addCleanup(gate.set)
        app.jobs.start("scan", work, platform="Commodore Amiga")

        def step(done, total, message="Reading a.chd (file 1/9)"):
            reported.clear()
            box["step"] = (done, total, message)
            self.assertTrue(reported.wait(5))
        return step

    def test_a_running_task_shows_in_the_header_with_its_share_and_what_it_does(self) -> None:
        step = self.run_job()                           # already running when the page loads, as after a click and a reload
        self.start(scanned=False)
        chip = "document.getElementById('head-activity-text').textContent"
        self.page.wait(f"{chip}.includes('Scanning')", timeout=20)
        self.assertFalse(self.page.eval("document.getElementById('topline').classList.contains('hidden')"))
        self.assertIn("Commodore Amiga", self.page.eval(chip))
        step(500, 1000)
        self.page.wait(f"{chip}.includes('50%')", timeout=10)
        self.click("#head-activity")
        self.page.wait("!!document.querySelector('.sheet')")
        self.assertIn("Reading a.chd (file 1/9)", self.page.eval("document.querySelector('.sheet').textContent"))
        self.no_js_errors()

    def test_while_the_build_is_worked_out_the_screen_says_so(self) -> None:
        self.stub({"path": "/api/library/plan", "method": "POST", "delay": 2500})
        self.start("#/system/commodore-amiga")
        self.page.wait("document.querySelector('#sys-build .tile-sub')?.textContent.includes('Working out')", timeout=30)
        self.page.wait("document.getElementById('head-activity-text').textContent.includes('Working out')", timeout=30)
        self.page.wait("document.querySelector('#sys-build .tile-sub')?.textContent.includes('will change')", timeout=60)
        self.no_js_errors()

    def test_while_the_last_scan_is_read_back_the_screen_says_so(self) -> None:
        self.stub({"path": "/api/scan/select", "method": "POST", "delay": 2500})
        self.page.goto(self.fx.url)
        self.scan()
        self.scan(platform="Nintendo Game Boy Advance", root=str(self.fx.gba))           # (the Amiga's scan is no longer the current one)
        self.page.goto(self.fx.url + "#/system/commodore-amiga")
        self.page.wait("document.getElementById('main').textContent.includes('Loading the last scan')", timeout=30)
        self.page.wait("!!document.getElementById('sys-build')", timeout=60)
        self.assertFalse(self.page.eval("!!document.getElementById('sys-scan')"))        # (never offers a scan of a folder it knows)
        self.no_js_errors()


class LayoutTests(UiTestCase):
    def test_every_screen_fits_the_window(self) -> None:
        self.start("#/")
        for width, height in ((1280, 720), (1280, 800), (1920, 1080)):
            self.page.call("Emulation.setDeviceMetricsOverride", width=width, height=height, deviceScaleFactor=1, mobile=False)
            for view in ("#/", "#/settings", "#/settings/rules", "#/settings/archive", "#/settings/emulators", "#/settings/disc",
                         "#/system/commodore-amiga", "#/system/commodore-amiga/files", "#/system/commodore-amiga/settings"):
                with self.subTest(width=width, view=view):
                    self.go(view, f"location.hash === {view!r}")
                    time.sleep(0.3)
                    over = self.page.eval("document.documentElement.scrollWidth - document.documentElement.clientWidth")
                    self.assertLessEqual(over, 0, "the page is wider than the window")
        self.no_js_errors()


class RobustnessTests(UiTestCase):
    """Failing answers and hostile names: a message, never an exception, never markup."""

    def test_a_failing_server_gives_messages_not_exceptions(self) -> None:
        self.stub({"path": "/api/library/defaults", "status": 500, "body": {"error": "RuntimeError: boom"}},
                  {"path": "/api/retroarch", "status": 500, "body": {"error": "RuntimeError: bang"}},
                  {"path": "/api/chdman", "status": 500, "body": {"error": "RuntimeError: crash"}})
        self.start("#/settings/rules", scanned=False)
        self.page.wait("document.getElementById('main').textContent.includes('boom')", timeout=30)
        self.go("#/settings/emulators", "document.getElementById('main').textContent.includes('bang')")
        self.go("#/settings/disc", "document.getElementById('main').textContent.includes('crash')")
        self.no_js_errors()

    def test_names_from_a_dat_are_shown_as_text_never_as_markup(self) -> None:
        evil = '<img src=x onerror=window.__xss=1><b id=xss-b>bold</b> & <script>window.__xss=2</script>'
        data = b"evil rom " * 500
        (self.fx.data / "nointro" / f"{GBA_DAT}.dat").write_text(
            f'clrmamepro (\n\tname "{GBA_DAT}"\n\tdescription "{GBA_DAT}"\n\tversion "20250101-000000"\n)\n'
            f'game (\n\tname "{evil} (USA)"\n\tdescription "{evil} (USA)"\n\trom ( name "{evil} (USA).gba" size {len(data)} '
            f'crc {zlib.crc32(data):08x} md5 {hashlib.md5(data).hexdigest()} sha1 {hashlib.sha1(data).hexdigest()} )\n)\n')
        odd = self.fx.gba / "it's \"odd\" & <i>co</i>"
        odd.mkdir(parents=True)
        (odd / "dump.gba").write_bytes(data)
        self.page.goto(self.fx.url)
        self.scan("Nintendo Game Boy Advance", str(self.fx.gba))
        self.page.goto(self.fx.url + "#/system/nintendo-game-boy-advance/files")
        self.page.wait("!!window.RomorgNext && document.querySelectorAll('#main [data-section=list] .row').length >= 1", timeout=60)
        self.page.eval(self.PAD)
        self.pad()
        self.tap("down")
        self.search("onerror")                    # (the new file name is made safe for the file system; the details carry the DAT's name)
        self.page.eval("document.querySelector('#main [data-section=list] button.row').focus()")
        self.page.wait("document.querySelector('#main .panel').textContent.includes('<i>co</i>')", timeout=30)      # (its folder, as text)
        self.click("#main [data-section=list] button.row")
        self.page.wait("!!document.querySelector('.sheet')")
        self.click(".sheet .row", "More details")
        self.page.wait("document.querySelector('.sheet')?.textContent.includes('<img src=x onerror')", timeout=30)
        self.page.eval("RomorgNext.press('B')")
        for view in ("missing", "excluded", "archive", "library"):
            self.go(f"#/system/nintendo-game-boy-advance/files?view={view}", f"document.querySelector('.views .chip.on')?.dataset.view === {view!r}")
            time.sleep(0.3)
        self.assertIsNone(self.page.eval("window.__xss === undefined ? null : window.__xss"))
        self.assertEqual(self.page.eval("document.querySelectorAll('img, #xss-b, main script').length"), 0)
        self.no_js_errors()


class RememberedScanTests(UiTestCase):
    """Going to another system and back needs no new scan."""

    def test_back_to_a_scanned_system_shows_its_scan(self) -> None:
        self.page.goto(self.fx.url)
        self.scan()
        self.scan(platform="Nintendo Game Boy Advance", root=str(self.fx.gba))
        self.page.goto(self.fx.url + "#/system/commodore-amiga")
        self.page.wait("!!document.getElementById('sys-build')", timeout=30)              # (restored: nobody pressed Scan)
        self.assertEqual(self.api("GET", "/api/status")["scan"]["platform"], "Commodore Amiga")
        self.go("#/system/nintendo-game-boy-advance", "document.getElementById('head-title').textContent.includes('Game Boy Advance') && !!document.getElementById('sys-build')")
        self.go("#/system/commodore-amiga", "document.getElementById('head-title').textContent.includes('Amiga') && !!document.getElementById('sys-build')")
        self.go("#/system/sega-mega-drive-genesis", "!!document.getElementById('sys-choose')")                  # no folder yet
        lynx = self.fx.tmp / "lynx"
        lynx.mkdir()
        self.api("POST", "/api/folders", {"platform": "Atari Lynx", "path": str(lynx)})
        self.go("#/system/atari-lynx", "!!document.getElementById('sys-scan')")                                 # never scanned
        self.no_js_errors()


class S3ArchiveUiTests(UiTestCase):
    """The S3 connection (the secret never shown again), its test, and the Archive view of Files as the bucket."""

    def setUp(self) -> None:
        super().setUp()
        from http.server import ThreadingHTTPServer
        import threading
        from test_s3 import AK, SK, FakeS3, _handler
        self.fake = FakeS3()
        self.s3srv = ThreadingHTTPServer(("127.0.0.1", 0), _handler(self.fake))
        threading.Thread(target=self.s3srv.serve_forever, daemon=True).start()
        self.addCleanup(self.s3srv.server_close)
        self.addCleanup(self.s3srv.shutdown)
        self.ak, self.sk = AK, SK

    def fill(self, id_: str, value: str) -> None:
        self.page.eval(f"""(() => {{ const i = document.getElementById({id_!r}); i.value = {value!r}; }})()""")

    def test_the_connection_is_set_up_tested_and_the_archive_view_is_the_bucket(self) -> None:
        self.fake.objects[f"arch/{self.fx.root.name}/_unmatched/notes.txt"] = (b"my notes", {})
        self.start("#/settings/archive/s3")
        self.page.wait("!!document.getElementById('s3-save')", timeout=30)
        self.fill("s3-endpoint", f"http://127.0.0.1:{self.s3srv.server_address[1]}")
        self.fill("s3-access", self.ak)
        self.fill("s3-secret", self.sk)
        self.fill("s3-bucket", "bucket")
        self.fill("s3-prefix", "arch")
        self.click("#s3-save")
        self.page.wait("document.querySelector('#main .panel').textContent.includes('Connected. The archive is s3://bucket/arch')", timeout=30)
        self.assertEqual(self.page.eval("document.getElementById('s3-secret').value"), "")
        self.assertNotIn(self.sk, self.page.eval("document.documentElement.outerHTML"))
        self.assertEqual(self.api("GET", "/api/status")["archive"]["dir"], "s3://bucket/arch/")
        self.go("#/settings/archive", "!!document.getElementById('arch-test')")
        self.assertIn("bucket/arch", self.main())
        self.click("#arch-test")
        self.page.wait("document.querySelector('#arch-test .row-value').textContent === 'Connected'", timeout=30)
        self.page.goto(self.fx.url + "#/settings/archive/s3")
        self.page.wait("document.getElementById('s3-secret')?.placeholder.includes('Saved')", timeout=30)
        self.go("#/system/commodore-amiga/files?view=archive", "document.querySelector('.crumb-path')?.textContent.includes('S3')")
        self.list_ready()
        self.assertIn("_unmatched", self.rows())
        self.no_js_errors()


class RestoreUiTests(UiTestCase):
    """Ticking files on the Archive view and restoring them (the archive keeps its copy)."""

    def test_several_ticked_files_are_restored_from_the_archive(self) -> None:
        arch = Path(tempfile.mkdtemp(prefix="romorg-ui-arch-"))
        self.addCleanup(shutil.rmtree, arch, True)
        mine = arch / self.fx.root.name / "_unmatched" / "notes"
        mine.mkdir(parents=True)
        (mine / "readme.txt").write_bytes(b"my notes")
        (mine / "todo.txt").write_bytes(b"later")
        self.page.goto(self.fx.url)
        self.set_archive(str(arch))
        self.start("#/system/commodore-amiga/files?view=archive")
        self.list_ready()
        self.click("#main [data-section=list] .row", "_unmatched")
        self.page.wait("document.querySelector('.crumb-path')?.textContent === '_unmatched'", timeout=30)
        self.click("#main [data-section=list] .row", "notes")
        self.page.wait("document.querySelector('.crumb-path')?.textContent === '_unmatched/notes'", timeout=30)
        self.tap("down")                                                                 # the first press shows the focus
        self.tap("Y")
        self.page.wait("document.querySelector('.panel-title')?.textContent === '1 ticked'")
        self.tap("down")
        self.tap("Y")
        self.page.wait("document.querySelector('.panel-title')?.textContent === '2 ticked'")
        self.page.eval("RomorgNext.press('Start')")
        self.page.wait("[...document.querySelectorAll('.toast')].some(t => t.textContent.includes('Restored 2'))", timeout=60)
        self.assertEqual((self.fx.root / "notes" / "readme.txt").read_bytes(), b"my notes")
        self.assertEqual((self.fx.root / "notes" / "todo.txt").read_bytes(), b"later")
        self.assertTrue((mine / "readme.txt").is_file())                                 # the archive keeps its copy
        self.no_js_errors()


class SavesUiTests(NoMachineFolders, UiTestCase):
    """RetroArch saves: nothing at all without a RetroArch config; with one the system shows them, and Settings › Saves has the choice."""

    def retroarch_with_saves(self) -> Path:
        """A RetroArch whose saves are in one folder, sorted per core: Amiga (PUAE) and Game Boy Advance (mGBA) saves."""
        base = Path(tempfile.mkdtemp(prefix="romorg-ui-sv-"))
        self.addCleanup(shutil.rmtree, base, True)
        ra, saves = base / "ra", base / "saves"
        (saves / "PUAE").mkdir(parents=True)
        (saves / "mGBA").mkdir()
        ra.mkdir()
        (ra / "retroarch.cfg").write_text(f'savefile_directory = "{saves}"\nsavestate_directory = "{saves}"\n'
                                          'sort_savefiles_enable = "true"\nsort_savestates_enable = "true"\n')
        for name in ("Alpha Quest v1.0 (1990)(Acme).srm", "Alpha Quest v1.0 (1990)(Acme).state1",         # the older edition: v1.1 supersedes it
                     "Theta Tower (1997)(Thor).srm"):                                                      # a game in the DAT you have no ROM of
            (saves / "PUAE" / name).write_bytes(b"s")
        (saves / "PUAE" / "Mystery (1999)(Nobody).srm").write_bytes(b"s")                                   # belongs to nothing known
        for name in ("Alpha Run (USA).srm", "Alpha Run (USA).state1", "Alpha Run (USA).state1.png", "Alpha Run (USA).state2",
                     "Alpha Run (Europe).srm", "Zeta Zap (Europe).srm"):
            (saves / "mGBA" / name).write_bytes(b"s")
        (ra / "info").mkdir()
        (ra / "info" / "puae_libretro.info").write_text('display_name = "Commodore - Amiga (PUAE)"\ncorename = "PUAE"\nsupported_extensions = "adf|ipf|dms"\n')
        (ra / "info" / "mgba_libretro.info").write_text('display_name = "Nintendo - Game Boy Advance (mGBA)"\ncorename = "mGBA"\nsupported_extensions = "gba"\n')
        self.api("POST", "/api/retroarch/select", {"custom": str(ra)})
        return saves

    def test_without_a_retroarch_config_there_is_no_trace_of_saves(self) -> None:
        self.start("#/system/commodore-amiga")
        self.page.wait("!!document.getElementById('sys-tiles') && !document.querySelector('#sys-tiles .loading')", timeout=60)
        self.assertFalse(self.page.eval("!!document.querySelector('[data-key=saves]')"))
        self.assertFalse(self.page.eval("!!document.querySelector('#main a[href$=\"/saves\"]')"))
        self.no_js_errors()

    def test_the_system_shows_its_saves_and_which_titles_they_match(self) -> None:
        self.retroarch_with_saves()
        self.api("POST", "/api/library/profile", {"platform": "Commodore Amiga", "prefer_saved": False})   # (else v1.0 is kept instead of v1.1)
        self.start("#/system/commodore-amiga")
        self.page.wait("!!document.querySelector('#sys-tiles [data-key=saves]')", timeout=60)
        self.assertIn("4", self.page.eval("document.querySelector('#sys-tiles [data-key=saves] .tile-number').textContent"))
        self.click("#main a[href$=\"/saves\"]")
        self.list_ready(3)
        text = self.page.eval("document.querySelector('#main [data-section=list]').textContent")
        for tag in ("Kept for its saves", "No ROM here", "No game"):                     # (what the next build does with each)
            self.assertIn(tag, text)
        self.click(".views .chip", "No game")
        self.page.wait("document.querySelectorAll('#main [data-section=list] .row').length === 2", timeout=30)
        self.assertEqual(sorted(n.split(" ")[0] for n in self.rows()), ["Mystery", "Theta"])
        self.no_js_errors()

    def test_the_saves_of_a_console_name_the_title_of_each(self) -> None:
        self.retroarch_with_saves()
        self.page.goto(self.fx.url)
        self.scan(platform="Nintendo Game Boy Advance", root=str(self.fx.gba))
        self.page.goto(self.fx.url + "#/system/nintendo-game-boy-advance/saves")
        self.list_ready(3)
        text = self.page.eval("document.querySelector('#main [data-section=list]').textContent")
        self.assertIn("Follows the game", text)
        self.assertIn("No ROM here", text)
        self.assertIn("Alpha Run", text)
        self.no_js_errors()

    def test_a_system_that_is_not_a_playstation_has_no_copy_for_another_region(self) -> None:
        self.retroarch_with_saves()
        self.start("#/system/commodore-amiga/saves")
        self.list_ready(3)
        self.pad()
        self.page.eval("document.querySelector('#main [data-section=list] .row').focus()")
        self.page.wait("!!document.getElementById('saves-change')", timeout=30)
        self.assertFalse(self.page.eval("!!document.getElementById('saves-copy')"))
        self.assertIn("Change what happens", self.strip())
        self.assertNotIn("Copy for another region", self.strip())
        self.tap("Y")                                                                    # (nothing)
        self.assertFalse(self.page.eval("!!document.querySelector('.sheet')"))
        self.click(".views .chip", "No game")                                            # a save of no game: no choice
        self.page.wait("document.querySelectorAll('#main [data-section=list] .row').length === 2", timeout=30)
        self.page.eval(f"{self._find('#main [data-section=list] .row', 'Mystery')}.focus()")
        self.page.wait("!document.getElementById('saves-change')", timeout=30)
        self.assertNotIn("Change what happens", self.strip())
        self.tap("A")
        time.sleep(0.3)
        self.assertFalse(self.page.eval("!!document.querySelector('.sheet')"))
        self.no_js_errors()

    def test_settings_has_the_choice_and_how_each_system_behaves(self) -> None:
        self.retroarch_with_saves()
        self.start("#/settings/saves", scanned=False)
        self.page.wait("!!document.querySelector('#main [data-key=saved]')", timeout=30)
        self.assertIn("Copy or convert", self.page.eval("document.querySelector('#main [data-key=saved] .row-value').textContent"))
        self.assertIn("PCSX2", self.main())
        self.click("#main [data-key=saved]")
        self.click(".sheet .row", "Keep both ROMs")
        self.page.wait("document.querySelector('#main [data-key=saved] .row-value').textContent.includes('Keep both ROMs')", timeout=30)
        self.assertEqual(self.api("GET", "/api/library/defaults")["profile"]["saved_games"], "keep")
        self.no_js_errors()

    def test_the_switch_that_renames_saves_is_one_setting_on_both_screens(self) -> None:
        self.retroarch_with_saves()
        self.start("#/settings/saves", scanned=False)
        row = "document.querySelector('#main [data-key=follow]')"
        self.page.wait(f"{row}?.getAttribute('aria-checked') === 'true'", timeout=30)
        self.page.eval(f"{row}.click()")
        self.page.wait(f"{row}?.getAttribute('aria-checked') === 'false'", timeout=30)
        self.assertFalse(self.api("GET", "/api/retroarch")["follow"])
        self.go("#/settings/emulators", f"{row}?.getAttribute('aria-checked') === 'false'")
        self.no_js_errors()


class EmulatorsUiTests(NoMachineFolders, UiTestCase):
    """Settings › Emulators: one row per emulator, found folders named, another folder can be chosen."""

    def test_found_folders_are_shown_and_another_can_be_chosen(self) -> None:
        home = Path(os.environ["HOME"])
        (home / ".config" / "PCSX2" / "inis").mkdir(parents=True)
        (home / ".config" / "PCSX2" / "inis" / "PCSX2.ini").write_text("[Folders]\n")
        other = home / "pcsx2-elsewhere"
        (other / "memcards").mkdir(parents=True)
        self.start("#/settings/emulators", scanned=False)
        row = "document.querySelector('#main a[href$=\"key=pcsx2\"]')"
        self.page.wait(f"!!{row}", timeout=30)
        self.assertIn("Found", self.page.eval(f"{row}.textContent"))
        self.assertIn(str(home / ".config" / "PCSX2"), self.page.eval(f"{row}.textContent"))
        self.assertIn("Not found", self.page.eval("document.querySelector('#main a[href$=\"key=dolphin\"]').textContent"))
        self.assertIn("Eden", self.page.eval("document.querySelector('#main a[href$=\"key=eden\"]').textContent"))
        self.click("#main a[href$=\"key=pcsx2\"]")
        self.page.wait(f"document.querySelector('.crumb-path')?.textContent === {str(home / '.config' / 'PCSX2')!r}", timeout=30)
        self.go("#/settings/emulators", f"!!{row}")
        self.no_js_errors()


class SavesScreenTests(NoMachineFolders, UiTestCase):
    """System › Saves of a PlayStation 2: what the next build does with each game's saves, the game's own choice, a copy for another region."""

    SLUG, PLATFORM = "sony-playstation-2", "Sony PlayStation 2"

    def setUp(self) -> None:
        super().setUp()
        import sqlite3
        import chdtestlib as T
        from romorg import paths
        from test_nintendo import ps2_iso
        images = {"Gamma (USA)": ps2_iso("SLUS_209.46"), "Delta (USA)": ps2_iso("SLUS_209.47")}
        T.write_dat(paths.redump_dir() / "Sony - PlayStation 2.dat", [(n, "Games", [d], "iso") for n, d in images.items()],
                    name="Sony - PlayStation 2", version="2026-06-15 03-41-38")
        db = paths.data_dir() / "nintendo" / "gametdb.sqlite"
        db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db)
        conn.executescript("CREATE TABLE IF NOT EXISTS names (kind TEXT, id TEXT, name TEXT); DELETE FROM names WHERE kind='ps2';")
        conn.executemany("INSERT INTO names VALUES ('ps2', ?, ?)", [("SLUS-20946", "Gamma"), ("SLES-50012", "Gamma"), ("SLPM-60001", "Gamma")])
        conn.commit()
        conn.close()
        self.addCleanup(db.unlink, True)
        self.roms = self.fx.tmp / "ps2"
        for name, iso in images.items():
            (self.roms / name).mkdir(parents=True)
            T.build_dvd_chd(self.roms / name / f"{name}.chd", iso)
        self.pcsx2 = self.fx.tmp / "pcsx2"
        self.card = self.pcsx2 / "memcards" / "Mcd001.ps2"
        for name in ("BASLUS-20946GAMMA", "BASLUS-20947DELTA", "BASLUS-29999LOST"):
            (self.card / name).mkdir(parents=True)
            (self.card / name / "data.bin").write_bytes(b"save")
        (self.pcsx2 / "inis").mkdir()
        (self.pcsx2 / "inis" / "PCSX2.ini").write_text("[Folders]\n")
        self.page.goto(self.fx.url)
        self.api("POST", "/api/chdman", {"engine": "python"})
        self.api("POST", "/api/emulators/config", {"source": "pcsx2", "folder": str(self.pcsx2)})
        self.scan(platform=self.PLATFORM, root=str(self.roms))
        self.open_saves()

    def open_saves(self) -> None:
        self.page.goto(self.fx.url + f"#/system/{self.SLUG}/saves")
        self.page.wait("!!window.RomorgNext", timeout=30)
        self.page.eval(self.PAD)
        self.list_ready(3)
        self.page.wait("document.querySelector('#main [data-section=panel]').textContent.includes('On the next build')", timeout=30)
        self.page.wait("!document.querySelector('#main [data-section=panel]').textContent.includes('Working it out')", timeout=60)
        self.pad()
        self.focus_row("Gamma")

    def focus_row(self, title: str) -> None:
        row = self._find("#main [data-section=list] .row", title)
        self.page.wait(f"!!{row}", timeout=30)
        self.page.eval(f"{row}.focus()")
        self.page.wait(f"document.querySelector('.panel-title')?.textContent === {title!r}", timeout=30)

    def panel(self) -> str:
        return self.page.eval("document.querySelector('#main [data-section=panel]').textContent")

    def row_text(self, title: str) -> str:
        return self.page.eval(f"{self._find('#main [data-section=list] .row', title)}.textContent")

    def sheet(self) -> str:
        self.page.wait("!!document.querySelector('.sheet')", timeout=30)
        return self.page.eval("document.querySelector('.sheet').textContent")

    def test_the_panel_says_what_the_next_build_does_and_what_a_rule_would_do(self) -> None:
        text = self.panel()
        for words in ("On the next build", "If a rule replaces it", "Copy or convert them to the new version (default)", "This release", "USA · SLUS-20946",
                      "Change what happens", "Copy for another region"):
            self.assertIn(words, text)
        self.assertIn("Follows the game", self.row_text("Gamma"))
        strip = self.strip()
        self.assertLess(strip.index("Change what happens"), strip.index("Copy for another region"))
        self.focus_row("Delta")                                                          # (the database has no other release of it)
        self.assertNotIn("Copy for another region", self.panel())
        self.assertNotIn("Copy for another region", self.strip())
        self.no_js_errors()

    def test_a_choice_is_posted_and_shown_and_the_default_clears_it(self) -> None:
        self.tap("A")
        offered = self.sheet()
        self.assertIn("If a rule replaces Gamma", offered)
        self.assertIn("Copy or convert them, from Settings › Saves", offered)
        self.page.wait("document.activeElement.dataset.current === '1'", timeout=30)     # (the focus is on the current one)
        self.assertIn("Use the default", self.page.eval("document.activeElement.textContent"))
        for _ in range(4):
            self.tap("down")
        self.assertIn("Archive the saves with the ROM", self.page.eval("document.activeElement.textContent"))
        self.tap("A")
        self.page.wait(f"{self._find('#main [data-section=list] .row', 'Gamma')}?.textContent.includes('Your choice')", timeout=30)
        self.page.wait("document.querySelector('#main [data-section=panel]').textContent.includes('Archive the saves with the ROM (only this game)')", timeout=30)
        self.page.wait("document.activeElement.textContent.startsWith('Gamma')", timeout=30)      # (the focus is back on the row)
        self.assertEqual(self.api("GET", "/api/library/profile?platform=Sony%20PlayStation%202")["profile"]["saved_overrides"],
                         [["Sony - PlayStation 2", "Gamma (USA)", "archive"]])
        self.tap("A")
        self.sheet()
        self.assertIn("Archive", self.page.eval("document.querySelector('.sheet .row[data-current]').textContent"))
        self.page.eval("document.querySelector('.sheet .row').click()")                  # Use the default
        self.page.wait(f"!{self._find('#main [data-section=list] .row', 'Gamma')}?.textContent.includes('Your choice')", timeout=30)
        self.page.wait("document.querySelector('#main [data-section=panel]').textContent.includes('(default)')", timeout=30)
        self.no_js_errors()

    def test_b_closes_a_sheet_and_nothing_is_posted(self) -> None:
        self.tap("A")
        self.sheet()
        self.tap("B")
        self.page.wait("!document.querySelector('.sheet')", timeout=30)
        self.assertNotIn("Your choice", self.row_text("Gamma"))
        self.tap("Y")
        self.assertIn("Experimental", self.sheet())
        self.tap("B")
        self.page.wait("!document.querySelector('.sheet')", timeout=30)
        self.assertEqual(sorted(p.name for p in self.card.iterdir()), ["BASLUS-20946GAMMA", "BASLUS-20947DELTA", "BASLUS-29999LOST"])
        self.no_js_errors()

    def test_y_copies_the_save_for_another_region_and_the_original_stays(self) -> None:
        self.tap("Y")
        offered = self.sheet()
        for words in ("Copy the save for another region", "Gamma, USA · SLUS-20946. Your save stays as it is.", "Europe", "SLES-50012", "Japan", "SLPM-60001",
                      "Experimental: a game that keeps the region inside its save may still refuse the copy."):
            self.assertIn(words, offered)
        self.assertIn("Make a copy", self.strip())
        self.tap("A")                                                                    # Europe, the first
        self.page.wait("document.getElementById('toasts').textContent.includes('1 save converted from SLUS-20946 to SLES-50012')", timeout=30)
        self.page.wait("document.querySelector('.panel-title')?.textContent === 'Gamma' && document.activeElement.textContent.startsWith('Gamma')", timeout=30)
        self.assertTrue(list(self.card.glob("BESLES-50012*")))
        self.assertTrue((self.card / "BASLUS-20946GAMMA" / "data.bin").is_file())
        self.no_js_errors()

    def test_a_running_emulator_and_a_refused_choice_are_said(self) -> None:
        self.stub({"path": "/api/saves/convert", "method": "POST", "body": {"skipped_running": True}},
                  {"path": "/api/library/saved", "method": "POST", "status": 409,
                   "body": {"error": "No save source is set up for this system, so saves are not looked at.", "code": "no_retroarch"}})
        self.page.goto("about:blank")
        self.open_saves()
        self.tap("Y")
        self.sheet()
        self.tap("A")
        self.page.wait("document.getElementById('toasts').textContent.includes('The emulator is running: close it and try again.')", timeout=30)
        self.page.wait("!document.querySelector('.sheet')", timeout=30)
        self.tap("A")
        self.sheet()
        self.tap("down")
        self.tap("A")
        self.page.wait("document.getElementById('toasts').textContent.includes('No save source is set up')", timeout=30)

    def test_the_panel_buttons_work_with_a_mouse(self) -> None:
        self.page.eval("RomorgNext.setMode('mouse')")
        self.click("#saves-change")
        self.assertIn("Use the default", self.sheet())
        self.page.eval("RomorgNext.press('B', 'mouse')")
        self.page.wait("!document.querySelector('.sheet')", timeout=30)
        self.click("#saves-copy")
        self.assertIn("Experimental", self.sheet())
        self.no_js_errors()


class SwitchUiTests(NoMachineFolders, UiTestCase):
    """The Nintendo Switch as a system: games, updates and add-ons in one folder, matched by title ID and version, with the saves of the
    emulators and the same Rules screen as any system."""

    def test_switch_is_a_system_with_its_games_and_saves(self) -> None:
        import struct
        from tests.test_switch import APP, small_db
        from romorg import switchdb
        base = Path(tempfile.mkdtemp(prefix="romorg-ui-sw-"))
        self.addCleanup(shutil.rmtree, base, True)
        path = switchdb.db_path()
        small_db(path)
        (base / "versions.json").write_text('{"%s": {"65536": "2020-01-01"}}' % APP)
        switchdb.build(path.with_name("t.json.gz"), path.with_name("c.json"), path, versions_json=base / "versions.json")
        self.assertEqual(switchdb.build_dat(), 1)
        entry = struct.pack("<QQI", 0, 1, 0) + bytes(4)

        def nsp(file_name: str, title_id: str) -> None:
            tik = f"{title_id}{'0' * 16}.tik".encode() + b"\0"
            (base / "games" / file_name).write_bytes(b"PFS0" + struct.pack("<II", 1, len(tik)) + bytes(4) + entry + tik + b"t")

        (base / "games").mkdir()
        nsp(f"Some Game [{APP}][v0].nsp", APP)
        nsp("Some Game update [0100AAAA0BBB0800][v65536].nsp", "0100AAAA0BBB0800")
        nsp("Not in the database [0100FFFF0BBB0000][v0].nsp", "0100FFFF0BBB0000")
        eden = base / "nand" / "user" / "save" / "0000000000000000" / ("A" * 32) / APP
        eden.mkdir(parents=True)
        (eden / "a.sav").write_bytes(b"1234")
        self.page.goto(self.fx.url)
        self.api("POST", "/api/emulators/config", {"source": "eden", "folder": str(base / "nand")})
        self.scan("Nintendo Switch", str(base / "games"))
        self.page.goto(self.fx.url + "#/system/nintendo-switch")
        self.page.wait("!!window.RomorgNext && !!document.querySelector('#sys-tiles [data-key=saves]')", timeout=60)
        self.page.eval(self.PAD)
        self.go("#/system/nintendo-switch/saves", "document.querySelectorAll('#main [data-section=list] .row').length >= 1")
        self.assertIn("Some Game", self.page.eval("document.querySelector('#main [data-section=list]').textContent"))
        # the game and its update are two entries of the catalogue, both had; the file the database does not know is unmatched
        self.go("#/system/nintendo-switch/files", "document.querySelectorAll('#main [data-section=list] .row').length >= 1")
        self.pad()
        self.tap("down")
        self.search("Some Game")
        names = self.rows()
        self.assertTrue(any("Update" in n for n in names), names)
        self.go("#/system/nintendo-switch/rules", "!!document.querySelector('#main [data-key=latest_only]')?.querySelector('.tag')")   # the same rules as any system
        self.no_js_errors()


class WhdloadUiTests(UiTestCase):
    def test_a_build_goes_on_to_the_whdload_games_by_itself(self) -> None:
        """The rule "Build WHDLoad games": the page starts that job when the library build ends (here there is no install to build)."""
        folder = self.fx.data / "whdinstalls"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "installs.json").write_text(json.dumps({"fetched": "2026-10-18", "fetched_at": time.time(), "installs": []}))
        arch = Path(tempfile.mkdtemp(prefix="romorg-ui-arch-"))
        self.addCleanup(shutil.rmtree, arch, True)
        self.stub()
        self.page.goto(self.fx.url)
        self.set_archive(str(arch))
        self.api("POST", "/api/library/defaults", {"build_whdload": True})
        self.start("#/system/commodore-amiga/build")
        self.page.wait("!!document.getElementById('build-start')", timeout=60)
        self.click("#build-start")
        self.page.wait("document.getElementById('main').textContent.includes('Library built')", timeout=120)
        sent = self.page.eval("window.__calls.filter(c => c.path === '/api/whdload/build').map(c => c.body)")
        self.assertEqual(sent, [{"platform": "Commodore Amiga"}])
        self.no_js_errors()

    def test_a_system_without_whdload_starts_no_such_build(self) -> None:
        arch = Path(tempfile.mkdtemp(prefix="romorg-ui-arch-"))
        self.addCleanup(shutil.rmtree, arch, True)
        self.stub()
        self.page.goto(self.fx.url)
        self.set_archive(str(arch))
        self.scan("Nintendo Game Boy Advance", str(self.fx.gba))
        self.page.goto(self.fx.url + "#/system/nintendo-game-boy-advance/build")
        self.page.wait("!!document.getElementById('build-start')", timeout=60)
        self.click("#build-start")
        self.page.wait("document.getElementById('main').textContent.includes('Library built')", timeout=120)
        self.assertEqual(self.page.eval("window.__calls.filter(c => c.path === '/api/whdload/build').length"), 0)
        self.no_js_errors()


if __name__ == "__main__":
    unittest.main()
