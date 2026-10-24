"""Real-browser checks of the UI (headless Chromium / Brave over the DevTools protocol).

Skipped unless a browser is available: set ROMORG_BROWSER_PORT to an already running ``--remote-debugging-port``, or have
Chromium / Chrome on the PATH, or (Steam Deck) the Brave flatpak. The app runs for real on a small synthetic Amiga library
(``tests/uifixture.py``); the checks click through the UI and fail on any JavaScript error."""
from __future__ import annotations

import os
import hashlib
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
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
    if cmd is None and os.name == "nt":       # Windows: not on PATH, but in its usual place (Edge is Chromium too)
        for var, rel in (("ProgramFiles", r"Google\Chrome\Application\chrome.exe"),
                         ("ProgramFiles(x86)", r"Google\Chrome\Application\chrome.exe"),
                         ("LOCALAPPDATA", r"Google\Chrome\Application\chrome.exe"),
                         ("ProgramFiles", r"BraveSoftware\Brave-Browser\Application\brave.exe"),
                         ("ProgramFiles(x86)", r"Microsoft\Edge\Application\msedge.exe"),
                         ("ProgramFiles", r"Microsoft\Edge\Application\msedge.exe")):
            exe = os.path.join(os.environ.get(var, ""), rel)
            if os.environ.get(var) and os.path.isfile(exe):
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
        # go of their files a moment after the main one exits (Windows refuses to delete an open file).
        for _ in range(20):
            shutil.rmtree(_profile, ignore_errors=True)
            if not os.path.exists(_profile):
                break
            time.sleep(0.25)


class UiTestCase(unittest.TestCase):
    def setUp(self) -> None:
        if _browser is None:
            self.skipTest(_unavailable or "no browser")
        self.fx = Fixture()
        self.addCleanup(self.fx.close)
        self.page = _browser.new_page()
        self.addCleanup(self.page.close)

    # -- helpers
    def open(self, hash_: str = "") -> None:
        self.page.goto(self.fx.url + hash_)
        self.page.eval("localStorage.clear()")
        self.page.goto(self.fx.url + hash_)

    def set_archive(self, folder: str, platform: str = "") -> None:
        """Choose the archive folder (there is no default one), the way Settings does; the page must be open."""
        body = {"dir": folder, **({"platform": platform} if platform else {})}
        self.page.eval("""(async()=>{const t=document.querySelector('meta[name=romorg-token]').content;
 const r = await fetch('/api/settings/archive',{method:'POST',headers:{'Content-Type':'application/json','X-Romorg-Token':t},body:JSON.stringify(%s)});
 return r.status})()""" % __import__("json").dumps(body))

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

    def files_ready(self, slug: str = "commodore-amiga", query: str = "") -> None:
        """The system's page, its list worked out and its tree drawn."""
        self.page.goto(self.fx.url + f"#/system/{slug}/files{query}")
        self.page.wait("document.getElementById('lib-live')?.textContent.includes('up to date')", timeout=60)
        self.page.wait("document.querySelectorAll('#files-tree .tree-row').length > 0 && !document.getElementById('files-tree').classList.contains('tree-busy')", timeout=60)

    def library_preview(self) -> None:
        self.open()
        self.scan()
        self.files_ready()

    def settings_of(self, slug: str = "commodore-amiga") -> None:
        """The system's Settings page, its rules drawn."""
        self.page.goto(self.fx.url + f"#/system/{slug}/settings")
        self.page.wait("document.querySelectorAll('#library-rules input').length > 3", timeout=30)

    def tree_rows(self) -> list[str]:
        """The names of the rows on screen (folders and files), top to bottom."""
        return self.page.eval("[...document.querySelectorAll('#files-tree .tree-row .tree-name')].map(e => e.textContent)")

    def open_folders(self) -> None:
        """Open every folder of the tree, level by level."""
        for _ in range(6):
            closed = self.page.eval("""(() => { const b = [...document.querySelectorAll('#files-tree button.tree-dir[aria-expanded=false]')];
              b.forEach(x => x.click()); return b.length; })()""")
            if not closed:
                return
            self.page.wait("!document.querySelector('#files-tree .totals-wait')", timeout=30)

    def open_file(self, name: str) -> None:
        """Press the file whose name has ``name`` in it: its details open."""
        self.open_folders()
        find = f"[...document.querySelectorAll('#files-tree button.tree-file')].find(b => b.querySelector('.tree-name').textContent.includes({name!r}))"
        self.page.wait(f"!!{find}", timeout=30)
        self.page.eval(f"{find}.click()")
        self.page.wait("!!document.querySelector('#files-tree .tree-detail:not(.hidden)')", timeout=30)

    def pick(self, key: str, value: str) -> None:
        """Press a filter chip (``show`` / ``state`` / ``region`` / ``language`` / ``rated``) and wait for the tree."""
        chip = f"[...document.querySelectorAll('#files-chips [data-filter={key}]')].find(b => b.dataset.value === {value!r})"
        self.page.wait(f"!!{chip}", timeout=30)
        self.page.eval(f"{chip}.click()")
        self.page.wait(f"{chip}?.classList.contains('active') && !document.getElementById('files-tree').classList.contains('tree-busy')", timeout=30)

    def side(self, which: str) -> None:
        tab = f"[...document.querySelectorAll('#files-sides .tab')].find(b => b.dataset.side === {which!r})"
        self.page.eval(f"{tab}.click()")
        self.page.wait(f"{tab}?.classList.contains('active') && !document.getElementById('files-tree').classList.contains('tree-busy')", timeout=30)

    def no_js_errors(self) -> None:
        self.assertEqual(self.page.errors, [])


class StartupTests(UiTestCase):
    def test_the_page_asks_for_status_and_platforms_once(self) -> None:
        self.open()
        self.page.wait("document.querySelectorAll('.nav-item[data-platform]').length > 3")
        names = self.page.eval("performance.getEntriesByType('resource').map(e => new URL(e.name).pathname)")
        self.assertEqual(names.count("/api/status"), 1)
        self.assertEqual(names.count("/api/platforms"), 1)
        self.no_js_errors()


class LibraryDefaultsTests(UiTestCase):
    """Your defaults (Settings) and a system's overrides (its Library tab): a rule follows the default, greyed, until it is overridden."""

    AMIGA = ".nav-item[data-platform=\"Commodore Amiga\"]"

    def api(self, method: str, path: str, body: dict | None = None) -> dict:
        import json
        import urllib.request
        req = urllib.request.Request(self.fx.url.rstrip("/") + path, method=method, data=json.dumps(body or {}).encode() if method == "POST" else None,
                                     headers={"X-Romorg-Token": "t", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())

    def library(self) -> None:
        self.page.goto(self.fx.url + "#/system/commodore-amiga/settings")
        self.page.wait("document.querySelectorAll('#library-rules .ov-unit').length > 3")

    def test_every_rule_follows_the_defaults_until_it_is_overridden_and_the_list_marks_the_system(self) -> None:
        self.open()
        self.page.wait("document.querySelectorAll('.nav-item[data-platform]').length > 3")
        self.library()
        self.assertEqual(self.page.eval("document.querySelectorAll('#library-rules .ov-unit.on').length"), 0)
        self.assertIn("Using your defaults for all", self.page.eval("document.getElementById('library-rules-note').textContent"))
        self.assertTrue(self.page.eval("document.getElementById('library-reset').disabled"))
        self.assertFalse(self.page.eval(f"!!document.querySelector('{self.AMIGA} .nav-override')"))
        # a rule that is not overridden is greyed and cannot be changed
        self.assertTrue(self.page.eval("document.querySelector('#library-rules .ov-unit.off .ov-body').hasAttribute('inert')"))
        self.click("#library-rules input[data-override=\"latest_only\"]")
        self.page.wait("!!document.querySelector('#library-rules .ov-unit.on[data-fields=\"latest_only\"]')")
        self.page.wait(f"!!document.querySelector('{self.AMIGA} .nav-override')")
        self.assertIn("1 rule is overridden", self.page.eval("document.getElementById('library-rules-note').textContent"))
        self.assertFalse(self.page.eval("document.getElementById('library-reset').disabled"))
        self.assertIn("this system only", self.page.eval("document.querySelector('#library-rules .ov-unit.on').textContent"))
        self.assertEqual(self.api("GET", "/api/platforms")[[p["name"] for p in self.api("GET", "/api/platforms")].index("Commodore Amiga")]["rule_overrides"],
                         ["latest_only"])
        # "use the default again" and "Reset all" both put the system back on your defaults
        self.click("#library-rules .ov-unit.on .linklike")
        self.page.wait("document.querySelectorAll('#library-rules .ov-unit.on').length === 0")
        self.page.wait(f"!document.querySelector('{self.AMIGA} .nav-override')")
        self.click("#library-rules input[data-override=\"latest_only\"]")
        self.page.wait("document.querySelectorAll('#library-rules .ov-unit.on').length === 1")
        self.click("#library-reset")
        self.page.wait("document.querySelectorAll('#library-rules .ov-unit.on').length === 0")
        self.assertEqual(self.api("GET", "/api/library/profile?platform=Commodore%20Amiga")["inherit"]["overridden"], [])
        self.no_js_errors()

    def test_the_amiga_only_rules_are_a_section_of_their_own_in_settings(self) -> None:
        self.open("#/settings")
        self.page.wait("!!document.querySelector('#set-library-rules #rules-amiga')", timeout=30)
        inside = self.page.eval("[...document.querySelectorAll('#set-library-rules #rules-amiga input[data-opt]')].map(i => i.dataset.opt).sort()")
        self.assertEqual(inside, ["best_variant", "borrow_other_editions", "build_whdload", "prefer_unprotected", "prefer_whdload", "rescue_only_dump"])
        self.assertTrue(self.page.eval("!!document.querySelector('#rules-amiga #rules-flags')"))                       # (the dump types too)
        outside = self.page.eval("[...document.querySelectorAll('#set-library-rules #rules-options input[data-opt]')].map(i => i.dataset.opt)")
        self.assertFalse({"best_variant", "borrow_other_editions", "prefer_whdload", "rescue_only_dump"} & set(outside), outside)
        self.assertIn("latest_only", outside)
        amiga_rules = ["bad_size", "cd_conversion", "faked", "modified", "unreleased", "virus"]                                   # (the exclusions only the Amiga systems have)
        self.assertEqual(self.page.eval("[...document.querySelectorAll('#rules-amiga #rules-exclude-amiga input[data-rule]')].map(i => i.dataset.rule).sort()"), amiga_rules)
        shared = self.page.eval("[...document.querySelectorAll('#set-library-rules #rules-exclude input[data-rule]')].map(i => i.dataset.rule)")
        self.assertFalse(set(amiga_rules) & set(shared), shared)
        self.assertIn("demo", shared)
        self.no_js_errors()

    def test_settings_has_the_region_order_and_two_regions_can_be_equally_best(self) -> None:
        self.open("#/settings")
        self.page.wait("document.querySelectorAll('#set-library-rules .rules-region .sortable-row').length > 3", timeout=30)
        first_two = self.page.eval("[...document.querySelectorAll('#set-library-rules .rules-region .sortable-row')].slice(0, 2).map(r => r.dataset.name)")
        self.assertTrue(self.page.eval("document.querySelector('#set-library-rules .rules-region .sortable-row .row-tie').disabled"))   # (the first has nothing above)
        self.page.eval("document.querySelectorAll('#set-library-rules .rules-region .row-tie')[1].click()")
        self.page.wait("document.querySelectorAll('#set-library-rules .rules-region .sortable-row.tied').length === 1")
        self.assertEqual(self.page.eval("[...document.querySelectorAll('#set-library-rules .rules-region .region-n')].slice(0, 3).map(e => e.textContent)"), ["1", "1", "2"])
        deadline = time.time() + 15
        while time.time() < deadline and self.api("GET", "/api/library/defaults")["profile"]["region_ties"] != [first_two[1]]:
            time.sleep(0.3)
        self.assertEqual(self.api("GET", "/api/library/defaults")["profile"]["region_ties"], [first_two[1]])
        self.no_js_errors()

    def test_the_page_says_whether_the_system_has_rules_of_its_own(self) -> None:
        self.open("#/system/commodore-amiga/files")
        self.page.wait("document.getElementById('totals-rules-line').textContent.includes('Your default rules')", timeout=30)
        self.api("POST", "/api/library/profile", {"platform": "Commodore Amiga", "latest_only": False})
        self.page.goto(self.fx.url + "#/system/commodore-amiga/files")
        self.page.wait("document.getElementById('totals-rules-line').textContent.includes('Custom')", timeout=30)
        self.assertIn("latest", self.page.eval("document.getElementById('totals-rules-line').textContent").lower())
        self.click("#totals-rules-line a")                                       # "Change" goes to the system's settings
        self.page.wait("document.getElementById('tabbtn-settings').getAttribute('aria-selected') === 'true'", timeout=30)
        self.no_js_errors()

    def test_a_system_has_one_page_and_its_settings_and_the_old_addresses_open_the_page(self) -> None:
        self.open()
        self.scan()
        self.assertEqual(self.page.eval("[...document.querySelectorAll('#sys-tabs .tab')].map(t => t.textContent)"), ["Files", "Settings"])
        for old in ("overview", "library", "browse", "tree", "browse?view=missing"):
            self.page.goto(self.fx.url + f"#/system/commodore-amiga/{old}")
            self.page.wait("document.getElementById('tabbtn-files').getAttribute('aria-selected') === 'true'", timeout=30)
        self.page.wait("[...document.querySelectorAll('#files-chips [data-filter=show]')].find(b => b.dataset.value === 'missing')?.classList.contains('active')", timeout=60)   # (the old Missing list: the filter)
        self.settings_of()
        panels = self.page.eval("[...document.querySelectorAll('#tab-settings > .panel > h2')].map(h => h.textContent)")
        self.assertEqual(panels[:3], ["Folder", "Library rules", "Building"])
        self.assertTrue(self.page.eval("!!document.getElementById('folder-input').offsetParent"))            # the folder is set here
        self.assertTrue(self.page.eval("document.getElementById('tab-files').classList.contains('hidden')"))
        self.no_js_errors()

    def test_the_tiles_follow_the_settings(self) -> None:
        self.open()
        self.scan()
        self.page.goto(self.fx.url + "#/system/commodore-amiga/files")
        self.page.wait("document.querySelectorAll('#sys-tiles .tile').length >= 2", timeout=30)
        self.assertEqual(self.page.eval("[...document.querySelectorAll('#sys-tiles .tile')].slice(0, 2).map(t => t.dataset.tile)"), ["have_all", "have_rules"])
        self.api("POST", "/api/settings/tiles", {"shown": ["last_scan", "have_all", "space"]})
        self.page.goto(self.fx.url + "#/system/commodore-amiga/files")
        self.page.wait("document.querySelectorAll('#sys-tiles .tile').length === 3 && !document.querySelector('#sys-tiles .totals-wait')", timeout=60)
        self.assertEqual(self.page.eval("[...document.querySelectorAll('#sys-tiles .tile')].map(t => t.dataset.tile)"), ["last_scan", "have_all", "space"])
        self.no_js_errors()

    def test_the_files_follow_a_rule_change_by_themselves(self) -> None:
        self.library_preview()
        before = self.api("POST", "/api/library/plan", {"limit": 1})["plan_id"]
        want = not self.api("GET", "/api/library/profile?platform=Commodore%20Amiga")["profile"]["latest_only"]
        self.settings_of()
        self.click("#library-rules input[data-override=\"latest_only\"]")
        self.page.wait("!!document.querySelector('#library-rules .ov-unit.on[data-fields=\"latest_only\"]')", timeout=30)
        self.click("#library-rules input[data-opt=\"latest_only\"]")
        deadline = time.time() + 15
        while time.time() < deadline and self.api("GET", "/api/library/profile?platform=Commodore%20Amiga")["profile"]["latest_only"] != want:
            time.sleep(0.3)
        self.page.eval("location.hash = '#/system/commodore-amiga/files'")           # back to the files: no button is pressed
        self.page.wait("document.getElementById('lib-live').textContent.includes('up to date') && document.getElementById('lib-apply-btn').dataset.stale === '0'", timeout=60)
        plan = self.api("POST", "/api/library/plan", {"limit": 1})
        self.assertEqual(plan["profile"]["latest_only"], want)
        self.assertNotEqual(plan["plan_id"], before)
        self.page.wait("document.querySelectorAll('#files-tree .tree-row').length > 0 && !document.getElementById('files-tree').classList.contains('tree-busy')", timeout=60)
        self.no_js_errors()

    def test_a_rule_only_some_systems_have_is_in_settings_and_only_on_those_systems(self) -> None:
        self.open("#/settings")
        self.page.wait("!!document.querySelector('#set-library-rules input[data-opt=\"keep_extras\"]')", timeout=30)      # the disc systems' rule: a default too
        self.assertFalse(self.page.eval("document.querySelector('#set-library-rules input[data-opt=\"keep_extras\"]').checked"))
        self.click("#set-library-rules input[data-opt=\"keep_extras\"]")
        deadline = time.time() + 15
        while time.time() < deadline and not self.api("GET", "/api/library/defaults")["profile"]["keep_extras"]:
            time.sleep(0.3)
        self.assertTrue(self.api("GET", "/api/library/defaults")["profile"]["keep_extras"])
        self.settings_of()
        self.assertFalse(self.page.eval("!!document.querySelector('#library-rules input[data-opt=\"keep_extras\"]')"))     # (the Amiga has no game folders)
        self.page.goto(self.fx.url + "#/system/sony-playstation/settings")
        self.page.wait("!!document.querySelector('#library-rules input[data-opt=\"keep_extras\"]')", timeout=30)
        self.assertTrue(self.page.eval("document.querySelector('#library-rules input[data-opt=\"keep_extras\"]').checked"))    # it follows the default
        self.no_js_errors()

    def test_the_files_are_a_tree_of_the_rom_folder_and_of_the_archive_with_filters(self) -> None:
        self.open("#/system/commodore-amiga/files")
        self.page.wait("!document.getElementById('lib-gate').classList.contains('hidden')", timeout=30)         # no scan yet: it says so
        self.scan()
        self.files_ready()
        self.assertEqual(self.page.eval("[...document.querySelectorAll('#files-sides .tab')].map(t => t.dataset.side)"), ["library", "archive"])
        self.assertIn("files", self.page.eval("document.getElementById('files-totals').textContent"))
        self.assertTrue(self.page.eval("document.getElementById('files-where').textContent.length > 3"))        # the folder's own path
        every = self.page.eval("document.querySelectorAll('#files-tree .tree-row').length")
        self.pick("show", "missing")
        self.assertTrue(self.page.eval("[...document.querySelectorAll('#files-tree .tree-file')].every(f => f.dataset.state === 'missing')"))
        self.assertIn("with these filters", self.page.eval("document.getElementById('files-totals').textContent"))
        self.click("#files-clear")
        self.page.wait(f"document.querySelectorAll('#files-tree .tree-row').length === {every}", timeout=30)
        self.page.eval("(() => { const q = document.getElementById('files-q'); q.value = 'zzz-nothing-is-called-this'; q.dispatchEvent(new Event('input')); })()")
        self.page.wait("document.getElementById('files-tree').textContent.includes('Nothing matches')", timeout=30)
        self.click("#files-clear")
        self.side("archive")
        self.assertFalse(self.page.eval("document.documentElement.scrollWidth > window.innerWidth + 2"))          # (no sideways scrolling)
        self.no_js_errors()

    def test_settings_lists_every_tile_to_tick_and_order(self) -> None:
        self.open("#/settings")
        self.page.wait("document.querySelectorAll('#set-tiles-list .sortable-row').length === 16", timeout=30)
        self.assertEqual(self.page.eval("document.querySelectorAll('#set-tiles-list .row-check:checked').length"), 9)
        self.page.eval("document.querySelector('#set-tiles-list .sortable-row[data-name=space] .row-check').click()")
        self.page.wait("document.getElementById('set-tiles-note').textContent.startsWith('10 of 10')")
        self.page.eval("document.querySelector('#set-tiles-list .sortable-row[data-name=regions] .row-check').click()")            # an eleventh: refused
        self.assertFalse(self.page.eval("document.querySelector('#set-tiles-list .sortable-row[data-name=regions] .row-check').checked"))
        deadline = time.time() + 15
        while time.time() < deadline and "space" not in self.api("GET", "/api/settings/tiles")["shown"]:
            time.sleep(0.3)
        self.assertEqual(self.api("GET", "/api/settings/tiles")["shown"][-1], "space")
        self.no_js_errors()

    def test_a_default_set_in_settings_reaches_the_systems_that_do_not_override_it(self) -> None:
        self.open("#/settings")
        self.page.wait("document.querySelectorAll('#set-library-rules input[data-opt]').length > 0 && document.querySelectorAll('.nav-item[data-platform]').length > 3")
        self.assertEqual(self.page.eval("document.querySelectorAll('#set-library-rules .ov-unit').length"), 0)       # (no Override boxes here)
        want = not self.page.eval("document.querySelector('#set-library-rules input[data-opt=\"latest_only\"]').checked")
        self.click("#set-library-rules input[data-opt=\"latest_only\"]")
        self.page.wait("document.getElementById('set-library-note').textContent.includes('differs')")
        self.assertEqual(self.api("GET", "/api/library/defaults")["profile"]["latest_only"], want)
        self.library()
        self.assertEqual(self.page.eval("document.querySelector('#library-rules input[data-opt=\"latest_only\"]').checked"), want)
        self.assertEqual(self.page.eval("document.querySelectorAll('#library-rules .ov-unit.on').length"), 0)
        self.page.goto(self.fx.url + "#/settings")
        self.page.wait("!document.getElementById('set-library-reset').disabled")
        self.click("#set-library-reset")
        self.page.wait("document.getElementById('set-library-reset').disabled")
        self.assertEqual(self.api("GET", "/api/library/defaults")["changed"], [])
        self.no_js_errors()


class BiosPageTests(UiTestCase):
    def test_the_bios_page_has_its_own_content_and_the_emulators_page_no_longer_has_the_check(self) -> None:
        self.open("#/bios")
        self.page.wait("document.querySelectorAll('#bios-sources tr').length === 2", timeout=30)
        self.assertEqual(self.page.eval("[...document.querySelectorAll('section.view:not(.hidden)')].map(e => e.id)"), ["view-bios"])
        self.assertIn("Keep BIOS", self.page.eval("document.getElementById('view-bios').textContent"))
        self.assertTrue(self.page.eval("document.getElementById('bios-link').getAttribute('aria-current') === 'true'"))
        self.assertFalse(self.page.eval("document.getElementById('view-retroarch').contains(document.getElementById('ra-bios-panel'))"))
        self.assertTrue(self.page.eval("document.getElementById('view-bios').contains(document.getElementById('ra-bios-panel'))"))
        self.page.eval("location.hash = '#/retroarch'")
        self.page.wait("document.getElementById('view-bios').classList.contains('hidden')")
        self.assertEqual(self.page.eval("[...document.querySelectorAll('section.view:not(.hidden)')].map(e => e.id)"), ["view-retroarch"])
        self.no_js_errors()

    def test_the_totals_and_the_numbers_of_the_scan_and_of_the_build_count_the_bios_files(self) -> None:
        self.open()
        self.scan()
        self.library_page()
        self.assertTrue(self.page.eval("[...document.querySelectorAll('#summary-cards .card')].some(c => c.textContent.includes('BIOS / firmware files'))"))   # (the scan's numbers: in Settings)
        self.assertTrue(self.page.eval("[...document.querySelectorAll('#lib-cards .card')].some(c => c.textContent.includes('BIOS / firmware files'))"))
        self.assertTrue(self.page.eval("['have_all', 'have_rules'].every(t => !!document.querySelector('#sys-tiles [data-tile=' + t + ']'))"))
        self.no_js_errors()

    def test_the_filter_shows_just_the_bios_files(self) -> None:
        import hashlib
        import zlib
        data = b"a firmware image, as far as the checksums go" * 20
        (self.fx.data / "dats" / "Atari Lynx - Firmware (TOSEC-v2012-09-08_CM).dat").write_text(
            '<?xml version="1.0"?><datafile><header><name>Atari Lynx - Firmware</name><version>2012-09-08</version></header>'
            f'<game name="Boot ROM"><description>x</description><rom name="boot.rom" size="{len(data)}" crc="{zlib.crc32(data):08x}" '
            f'md5="{hashlib.md5(data).hexdigest()}" sha1="{hashlib.sha1(data).hexdigest()}"/></game></datafile>')
        (self.fx.root / "somewhere.adf").write_bytes(data)
        self.open()
        self.scan()
        self.library_page()
        self.page.wait("[...document.querySelectorAll('#summary-cards .card')].some(c => /^1\\s*BIOS/.test(c.textContent.trim()))", timeout=30)
        self.pick("show", "bios")
        self.page.wait("document.querySelectorAll('#files-tree .tree-file').length === 1", timeout=30)
        self.assertIn("BIOS", self.page.eval("document.querySelector('#files-tree .tree-file').textContent"))
        self.no_js_errors()

    def library_page(self) -> None:
        self.files_ready()
        self.page.wait("document.querySelectorAll('#lib-cards .card').length > 3", timeout=60)

    def test_the_rule_is_an_option_with_an_override_box_like_the_others(self) -> None:
        self.open()
        self.page.goto(self.fx.url + "#/system/commodore-amiga/settings")
        self.page.wait("!!document.querySelector('#library-rules input[data-opt=\"keep_bios\"]')", timeout=30)
        self.assertTrue(self.page.eval("document.querySelector('#library-rules input[data-opt=\"keep_bios\"]').checked"))
        self.assertTrue(self.page.eval("!!document.querySelector('#library-rules input[data-override=\"keep_bios\"]')"))
        self.page.goto(self.fx.url + "#/settings")
        self.page.wait("!!document.querySelector('#set-library-rules input[data-opt=\"keep_bios\"]')", timeout=30)
        self.assertTrue(self.page.eval(r"""['min_rating', 'top_n', 'min_votes'].every(f => !!document.querySelector('#set-library-rules input[data-field="' + f + '"]'))"""))   # the ratings filter
        self.no_js_errors()


class SidebarTests(UiTestCase):
    def test_the_progress_bar_of_a_running_scan_sits_under_the_name_and_the_figures(self) -> None:
        self.open()
        self.scan()
        self.api_post("/api/library/profile", {"platform": "Commodore Amiga", "override": {"fields": ["latest_only"], "on": True}})
        self.page.eval("location.reload()")
        self.page.wait("!!document.querySelector('.nav-item[data-platform=\"Commodore Amiga\"] .nav-pct')?.textContent && "
                       "!!document.querySelector('.nav-item[data-platform=\"Commodore Amiga\"] .nav-override')", timeout=30)
        self.page.eval("""(() => { const i = document.querySelector('.nav-item[data-platform="Commodore Amiga"]');
            i.classList.add('running'); i.querySelector('.nav-job').classList.remove('hidden');
            i.querySelector('.progress-bar').style.width = '40%'; })()""")
        got = self.page.eval("""(() => { const i = document.querySelector('.nav-item[data-platform="Commodore Amiga"]');
            const r = (sel) => i.querySelector(sel).getBoundingClientRect(), item = i.getBoundingClientRect();
            return {name: r('.nav-name').bottom, meta: r('.nav-meta').bottom, metaTop: r('.nav-meta').top, nameTop: r('.nav-name').top,
                    jobTop: r('.nav-job').top, jobBottom: r('.nav-job').bottom, itemBottom: item.bottom, itemRight: item.right,
                    jobRight: r('.nav-job .progress').right, metaRight: r('.nav-meta').right, metaLeft: r('.nav-meta').left,
                    nameRight: r('.nav-name').right}; })()""")
        self.assertGreaterEqual(got["jobTop"] + 0.5, max(got["name"], got["meta"]), got)          # the bar is below both
        self.assertLessEqual(got["jobBottom"], got["itemBottom"] + 0.5, got)
        self.assertLessEqual(abs(got["metaTop"] - got["nameTop"]), 12, got)                      # figures on the name's line, not under it
        self.assertLessEqual(got["nameRight"], got["metaLeft"] + 0.5, got)
        self.assertLessEqual(got["jobRight"], got["itemRight"] + 0.5, got)
        self.no_js_errors()

    def api_post(self, path: str, body: dict) -> None:
        self.page.eval("""(async () => { const t = document.querySelector('meta[name=romorg-token]').content;
            await fetch(%r, {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Romorg-Token': t}, body: JSON.stringify(%s)}); })()"""
                       % (path, __import__("json").dumps(body)))
        time.sleep(0.5)


class DatabasesPanelTests(UiTestCase):
    def test_every_database_is_listed_with_its_address_and_version(self) -> None:
        self.open()
        self.page.wait("document.getElementById('updates-line').textContent.includes('Redump')")
        line = self.page.eval("document.getElementById('updates-line').textContent")
        for source in ("TOSEC", "No-Intro", "Redump"):
            self.assertIn(source, line)
        self.assertFalse(self.page.eval("!!document.getElementById('databases-btn')"))               # (not in the More menu any more)
        self.assertEqual(self.page.eval("document.getElementById('databases-link').closest('aside') !== null"), True)
        self.click("#databases-link")
        self.page.wait("document.querySelectorAll('#databases-body tbody tr').length > 12", timeout=30)
        rows = self.page.eval("""[...document.querySelectorAll('#databases-body tbody tr')].map(r => ({
            source: r.cells[0].textContent.trim(), name: r.cells[1].querySelector('a').textContent.trim(),
            url: r.cells[1].querySelector('a').href, used: r.cells[2].textContent.trim(), installed: r.cells[3].textContent.trim(),
            status: r.cells[5].textContent.trim()}))""")
        names = {r["name"]: r for r in rows}
        for wanted in ("Sega - Dreamcast", "Sony - PlayStation 2", "Nintendo - Wii", "Nintendo - Game Boy Advance",
                       "Nintendo Switch: games, every update version, add-ons", "Nintendo Wii U: the disc games, with region and languages"):
            self.assertIn(wanted, names)
        self.assertEqual(names["Nintendo - Game Boy Advance"]["installed"], "20250101-000000")     # the fixture's DAT version
        self.assertEqual(names["Sega - Dreamcast"]["installed"], "not installed")
        self.assertEqual(names["Nintendo - Wii"]["url"], "https://raw.githubusercontent.com/libretro/libretro-database/master/metadat/redump/Nintendo%20-%20Wii.dat")
        self.assertTrue(names["Nintendo - Game Boy Advance"]["url"].endswith("Nintendo%20-%20Game%20Boy%20Advance.dat"))
        self.assertIn("Nintendo Wii U", names["Nintendo Wii U: the disc games, with region and languages"]["used"])
        self.assertEqual({r["source"] for r in rows} >= {"TOSEC", "No-Intro", "Redump", "titledb", "GameTDB", "PCSX2", "LaunchBox", "libretro", "WHDLoad"}, True)
        self.assertTrue(all(r["url"].startswith("http") for r in rows))
        self.assertEqual(next(r for r in rows if r["source"] == "libretro")["status"], "ships with the app")
        self.assertNotIn("no-intro.org", self.page.eval("document.getElementById('databases-body').textContent"))
        # leaving Settings for Databases must not leave Settings on the page
        self.page.eval("location.hash = '#/settings'")
        self.page.wait("!document.getElementById('view-settings').classList.contains('hidden')")
        self.click("#databases-link")
        self.page.wait("document.getElementById('view-settings').classList.contains('hidden')")
        self.assertEqual(self.page.eval("[...document.querySelectorAll('.view:not(.hidden)')].map(v => v.id)"), ["view-databases"])
        self.no_js_errors()


class FileDetailTests(UiTestCase):
    def test_a_file_opens_into_what_is_known_about_it_with_its_checksums(self) -> None:
        self.library_preview()
        self.open_file("Alpha Quest")
        panel = "document.querySelector('#files-tree .tree-detail:not(.hidden)')"
        self.page.wait(f"{panel}.querySelector('.cs-sums table, .cs-sums .cs-note')", timeout=30)
        text = self.page.eval(f"{panel}.innerText")
        self.assertRegex(text.lower(), r"kept|excluded|better version|set aside")
        self.assertRegex(text.lower(), r"sha-?1|crc")
        self.assertIn("The build", text)
        self.assertTrue(self.page.eval(f"!!{panel}.querySelector('.copy-chip.name')"))               # the game's name can be copied
        self.no_js_errors()

    def test_a_kept_file_says_so_and_shows_the_better_version_you_lack(self) -> None:
        eu, us = b"europe rom " * 400, b"usa rom " * 400
        games = "".join(
            f'game (\n\tname "Solo Quest ({r})"\n\tdescription "Solo Quest ({r})"\n\trom ( name "Solo Quest ({r}).gba" size {len(d)} '
            f'crc {zlib.crc32(d):08x} md5 {hashlib.md5(d).hexdigest()} sha1 {hashlib.sha1(d).hexdigest()} )\n)\n'
            for r, d in (("Europe", eu), ("USA", us)))
        (self.fx.data / "nointro" / f"{GBA_DAT}.dat").write_text(
            f'clrmamepro (\n\tname "{GBA_DAT}"\n\tdescription "{GBA_DAT}"\n\tversion "20250101-000000"\n)\n' + games)
        (self.fx.gba / "Solo Quest (USA).gba").write_bytes(us)
        self.open()
        self.scan("Nintendo Game Boy Advance", str(self.fx.gba))
        self.files_ready("nintendo-game-boy-advance")
        self.open_folders()
        self.page.wait("document.querySelectorAll('#files-tree .tree-file .tree-upgrade').length === 1", timeout=30)    # (the blue arrow: a better version exists)
        self.open_file("Solo Quest (USA)")
        panel = "document.querySelector('#files-tree .tree-detail:not(.hidden)')"
        self.page.wait(f"{panel}.innerText.includes('A better match')", timeout=30)
        text = self.page.eval(f"{panel}.innerText")
        self.assertIn("Solo Quest (Europe)", text)
        self.assertIn(hashlib.sha1(eu).hexdigest(), text)
        self.assertNotIn("no rule sets", text)
        self.no_js_errors()

    def test_a_folder_says_how_many_games_below_it_have_a_better_version(self) -> None:
        from romorg import server
        with mock.patch.object(server.App, "_upgrade_names", return_value={"x"}), mock.patch.object(server.App, "_has_upgrade", return_value=True):
            self.library_preview()
            self.page.wait("document.querySelectorAll('#files-tree .tree-dir .tree-upgrade').length > 0", timeout=30)
            text = self.page.eval("document.querySelector('#files-tree .tree-dir .tree-upgrade').textContent")
            self.assertRegex(text, r"^⬆ \d+$")
        self.no_js_errors()

    def test_the_details_of_a_game_you_lack_show_no_stray_text(self) -> None:
        self.library_preview()
        self.pick("show", "missing")
        self.open_folders()
        self.page.eval("document.querySelector('#files-tree button.tree-file').click()")
        self.page.wait("!!document.querySelector('#files-tree .tree-detail:not(.hidden) .tree-facts')", timeout=30)
        text = self.page.eval("document.querySelector('#files-tree .tree-detail:not(.hidden)').innerText")
        self.assertIn("Not in your folder", text)
        self.assertNotIn("null", text)                                # (a game with no file has no buttons: nothing is written in their place)
        self.no_js_errors()

    def test_always_exclude_a_game_from_its_details_and_the_tree_follows(self) -> None:
        self.library_preview()
        target = "Beta Blaster (1992)(Bits).adf"
        self.open_file("Beta Blaster")
        self.page.eval("""[...document.querySelectorAll('#files-tree .tree-detail:not(.hidden) button')].find(b => b.textContent === 'Always exclude').click()""")
        self.page.wait("[...document.querySelectorAll('.toast')].some(t => t.textContent.includes('the list is updating'))", timeout=30)
        self.page.wait("document.getElementById('lib-live').textContent.includes('up to date') && !document.getElementById('files-tree').classList.contains('tree-busy')", timeout=60)
        plan = self.api("POST", "/api/library/plan", {"limit": 500})
        row = [r for r in plan["items"] if r.get("from", "").endswith(target)][0]
        self.assertEqual(row["category"], "excluded")
        self.assertIn("always exclude", row["reason"].lower())
        self.no_js_errors()

    def api(self, method: str, path: str, body: dict | None = None) -> dict:
        import json
        import urllib.request
        req = urllib.request.Request(self.fx.url.rstrip("/") + path, method=method, data=json.dumps(body or {}).encode() if method == "POST" else None,
                                     headers={"X-Romorg-Token": "t", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())


class BuildElsewhereTests(UiTestCase):
    def confirm_dialog(self) -> str:
        self.page.wait("!document.getElementById('confirm-modal').classList.contains('hidden')")
        text = self.page.eval("document.getElementById('confirm-body').innerText")
        self.page.eval("document.getElementById('confirm-yes').click()")
        return text

    def test_build_in_another_folder_leaves_the_source_alone(self) -> None:
        dest = Path(tempfile.mkdtemp(prefix="romorg-ui-lib-"))
        self.addCleanup(shutil.rmtree, dest, True)
        before = sorted(str(p.relative_to(self.fx.root)) for p in self.fx.root.rglob("*"))
        self.open()
        self.scan()
        self.page.goto(self.fx.url + "#/system/commodore-amiga/settings")                    # where a build puts things is a setting of the system
        self.page.wait("!!document.querySelector('#lib-where-other')?.offsetParent")
        self.assertTrue(self.page.eval("document.getElementById('lib-export-opts').classList.contains('hidden')"))
        self.page.eval("document.getElementById('lib-where-other').click()")
        self.page.eval(f"""(() => {{ const i = document.getElementById('lib-export-dest'); i.value = {str(dest)!r};
            i.dispatchEvent(new Event('change')); }})()""")
        self.assertEqual(self.page.eval("document.getElementById('lib-apply-btn').textContent"), "Build library in destination")
        self.page.eval("location.hash = '#/system/commodore-amiga/files'")
        self.page.wait("document.getElementById('lib-cards').textContent.includes('To copy')", timeout=60)
        self.click("#lib-apply-btn")
        self.assertIn("its files stay where they are", self.confirm_dialog())
        self.page.wait("document.querySelector('.toast')?.textContent.includes('Built ')", timeout=60)
        built = [p for p in dest.rglob("*") if p.is_file() and ".romorg-library" not in p.parts]
        self.assertTrue(built)
        self.assertEqual(sorted(str(p.relative_to(self.fx.root)) for p in self.fx.root.rglob("*")), before)
        self.page.goto(self.fx.url + "#/system/commodore-amiga/settings")   # the choice is remembered
        self.page.wait("document.getElementById('lib-where-other').checked")
        self.assertEqual(self.page.eval("document.getElementById('lib-export-dest').value"), str(dest))
        self.assertFalse(self.page.eval("!!document.getElementById('lib-undo-btn')"))          # (no Undo: removed in 0.4)
        self.no_js_errors()



class NoMachineFolders:
    """Mixin: no emulator is installed for the test (what this machine has must not be found and selected)."""

    def setUp(self) -> None:
        super().setUp()
        home = Path(tempfile.mkdtemp(prefix="romorg-ui-home-"))
        self.addCleanup(shutil.rmtree, home, True)
        env = mock.patch.dict(os.environ, {"HOME": str(home), "APPDATA": "", "USERPROFILE": ""})
        env.start()
        self.addCleanup(env.stop)


class RetroArchTests(UiTestCase):
    def confirm_dialog(self) -> str:
        self.page.wait("!document.getElementById('confirm-modal').classList.contains('hidden')")
        text = self.page.eval("document.getElementById('confirm-body').innerText")
        self.page.eval("document.getElementById('confirm-yes').click()")
        return text

    def fill(self, id_: str, value: str) -> None:
        self.page.eval(f"""(() => {{ const i = document.getElementById({id_!r}); i.value = {value!r};
            i.dispatchEvent(new Event('change')); }})()""")

    def test_move_saves_and_update_the_config(self) -> None:
        base = Path(tempfile.mkdtemp(prefix="romorg-ui-ra-"))
        self.addCleanup(shutil.rmtree, base, True)
        ra, old, new = base / "ra", base / "old", base / "new"
        ra.mkdir()
        (old / "bsnes").mkdir(parents=True)
        (old / "bsnes" / "Mario (USA).srm").write_bytes(b"s")
        (old / "bsnes" / "Mario (USA).state1").write_bytes(b"t")
        cfg = ra / "retroarch.cfg"
        cfg.write_text(f'savefile_directory = "{old}"\nsavestate_directory = "{old}"\nsort_savefiles_enable = "true"\nsort_savestates_enable = "true"\n')
        self.open("#/retroarch")
        self.page.wait("!document.getElementById('view-retroarch').classList.contains('hidden')")
        self.fill("ra-custom", str(ra))
        self.click("#ra-custom-add")
        self.page.wait(f"document.getElementById('ra-install').value === {str(cfg)!r}")
        self.assertEqual(self.page.eval("document.getElementById('ra-save-dir').value"), str(old))
        self.fill("ra-save-dir", str(new / "saves"))
        self.fill("ra-state-dir", str(new / "states"))
        self.click("#ra-plan-btn")
        self.page.wait("document.getElementById('ra-cards').textContent.includes('Files to move')")
        self.assertTrue((old / "bsnes" / "Mario (USA).srm").is_file())
        self.click("#ra-apply-btn")
        self.assertIn("A zip backup", self.confirm_dialog())
        self.page.wait("document.querySelector('.toast')?.textContent.includes('Moved 2')", timeout=60)
        self.assertTrue((new / "saves" / "bsnes" / "Mario (USA).srm").is_file())
        self.assertTrue((new / "states" / "bsnes" / "Mario (USA).state1").is_file())
        self.assertIn(str(new / "saves"), cfg.read_text())
        self.assertFalse((old / "bsnes" / "Mario (USA).srm").exists())
        self.assertFalse(self.page.eval("!!document.getElementById('ra-undo-btn')"))
        self.no_js_errors()

    def test_bios_check_finds_a_file_and_places_it(self) -> None:
        base = Path(tempfile.mkdtemp(prefix="romorg-ui-bios-"))
        self.addCleanup(shutil.rmtree, base, True)
        ra = base / "ra"
        (ra / "cores").mkdir(parents=True)
        (ra / "retroarch.cfg").write_text(f'system_directory = "{ra}/system"\n')
        (ra / "cores" / "x_libretro.info").write_text('display_name = "Commodore - Amiga (X)"\ncorename = "X"\nsupported_extensions = "adf"\n'
                                                      'firmware_count = 1\nfirmware0_path = "kick34005.A500"\nfirmware0_opt = "false"\n')
        (self.fx.root / "kick34005.A500").write_bytes(b"kick")
        self.open("#/retroarch")
        self.page.wait("!document.getElementById('view-retroarch').classList.contains('hidden')")
        self.page.eval("""(async()=>{const t=document.querySelector('meta[name=romorg-token]').content;
         const h={'Content-Type':'application/json','X-Romorg-Token':t};
         await fetch('/api/folders',{method:'POST',headers:h,body:JSON.stringify({platform:'Commodore Amiga',path:%r})});})()""" % str(self.fx.root))
        self.fill("ra-custom", str(ra))
        self.click("#ra-custom-add")
        self.page.wait(f"document.getElementById('ra-install').value === {str(ra / 'retroarch.cfg')!r}")
        self.page.eval("document.getElementById('ra-bios-platform').value = 'Commodore Amiga'")
        self.click("#ra-bios-check")
        self.page.wait("document.getElementById('ra-bios-table').textContent.includes('found - not in place')")
        self.page.eval("document.getElementById('ra-bios-mode').value = 'copy'")
        self.click("#ra-bios-apply")
        self.confirm_dialog()
        self.page.wait("document.getElementById('ra-bios-table').textContent.includes('there (no checksum')", timeout=30)
        self.assertEqual((ra / "system" / "kick34005.A500").read_bytes(), b"kick")
        self.no_js_errors()




class SavesUiTests(NoMachineFolders, UiTestCase):
    """RetroArch saves in the UI (Amendment 31): nothing at all without a RetroArch config; with one the Overview, the Library
    tab and the Browse tab show them, and the default / per-game choice is stored."""
    SAVES_SHOTS = os.environ.get("ROMORG_SHOT_DIR", "")

    def api(self, method: str, path: str, body: dict | None = None) -> dict:
        import json
        import urllib.request
        req = urllib.request.Request(self.fx.url.rstrip("/") + path, method=method, data=json.dumps(body or {}).encode() if method == "POST" else None,
                                     headers={"X-Romorg-Token": "t", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())

    def select(self, id_: str, value: str) -> None:
        self.page.eval(f"""(() => {{ const s = document.getElementById({id_!r}); s.value = {value!r}; s.dispatchEvent(new Event('change')); }})()""")

    def shot(self, name: str, width: int = 1280) -> None:
        if self.SAVES_SHOTS:
            self.page.screenshot(str(Path(self.SAVES_SHOTS) / f"{name}-{width}.png"), width=width)

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

    def library_open(self) -> None:
        self.files_ready()

    # ---- no RetroArch config: nothing at all
    def test_without_a_retroarch_config_there_is_no_trace_of_saves_anywhere(self) -> None:
        self.open()
        self.scan()
        self.library_open()
        self.assertTrue(self.page.eval("document.getElementById('ov-saves').classList.contains('hidden')"))
        self.assertEqual(self.page.eval("document.getElementById('ov-saves').textContent"), "")
        self.assertTrue(self.page.eval("document.getElementById('saves-box').classList.contains('hidden')"))
        self.assertFalse(self.page.eval("!!document.querySelector('#files-chips [data-filter=show][data-value=saves]')"))
        self.open_file("Alpha Quest")
        self.assertNotIn("Saves", self.page.eval("document.querySelector('#files-tree .tree-detail:not(.hidden) .tree-facts').textContent"))
        self.settings_of()
        self.assertFalse(self.page.eval("!!document.getElementById('lib-saved-games')"))
        self.assertNotIn("aves", self.page.eval("document.getElementById('library-rules').textContent").replace("Saves and", ""))
        self.page.goto(self.fx.url + "#/settings")
        self.page.wait("document.getElementById('set-library-rules').children.length > 0 && !document.getElementById('set-library-rules').textContent.includes('Loading')")
        self.assertNotIn("Saves (", self.page.eval("document.getElementById('set-library-rules').textContent"))
        self.no_js_errors()

    # ---- with one
    def test_the_page_says_how_many_saves_and_which_titles_they_match(self) -> None:
        self.retroarch_with_saves()
        self.api("POST", "/api/library/profile", {"platform": "Commodore Amiga", "prefer_saved": False})   # (else v1.0 is kept instead of v1.1)
        self.open()
        self.scan()
        self.library_open()
        self.page.wait("!document.getElementById('ov-saves').classList.contains('hidden')")
        self.assertEqual(self.page.eval("document.getElementById('ov-saves-line').textContent"),
                         "4 save files for 3 games (1 with a ROM here, 1 without, 1 unmatched)")
        self.assertIn("3 saves \u00b7 1 state", self.page.eval("document.getElementById('ov-saves').textContent"))
        self.page.wait("!!document.getElementById('ov-saves-plan')")
        self.assertIn("keep 1 game the rules would archive", self.page.eval("document.getElementById('ov-saves-plan').textContent"))
        # the saves the emulator keeps, and the game each belongs to: at the foot of the page
        self.assertFalse(self.page.eval("document.getElementById('saves-box').classList.contains('hidden')"))
        self.assertIn("4 files for 3 games", self.page.eval("document.getElementById('saves-box-title').textContent"))
        self.page.eval("document.getElementById('saves-box').open = true")
        self.page.wait("document.querySelectorAll('#saves-table tbody tr:not(.detail-row)').length === 3", timeout=30)
        text = self.page.eval("document.getElementById('saves-table').textContent")
        self.assertIn("ROM here", text)
        self.assertIn("title, no ROM here", text)
        self.shot("files")
        self.shot("files", 700)
        self.no_js_errors()

    def test_the_system_list_shows_a_saves_badge_after_a_scan(self) -> None:
        self.retroarch_with_saves()
        self.open()
        self.assertFalse(self.page.eval("!!document.querySelector('.nav-item[data-platform=\"Commodore Amiga\"] .nav-saves')"))
        self.scan()
        self.page.eval("location.reload()")                                               # the list is read again
        self.page.wait("!!document.querySelector('.nav-item[data-platform=\"Commodore Amiga\"] .nav-saves')", timeout=30)
        self.assertEqual(self.page.eval("document.querySelector('.nav-item[data-platform=\"Commodore Amiga\"] .nav-saves').textContent"), "4 saves")
        self.assertFalse(self.page.eval("!!document.querySelector('.nav-item[data-platform=\"Nintendo Game Boy Advance\"] .nav-saves')"))
        self.no_js_errors()

    def test_the_saves_have_a_page_of_their_own_with_the_choice_and_how_each_system_behaves(self) -> None:
        self.retroarch_with_saves()
        self.open()
        self.scan()
        self.open("#/saves")
        self.page.wait("!!document.querySelector('#saves-page-rules #set-saved-games')", timeout=30)
        self.assertEqual(self.page.eval("document.querySelector('#set-saves-group .rules-head').textContent"), "Saves")
        self.assertEqual(self.page.eval("document.getElementById('set-saved-games').value"), "convert")
        self.page.wait("document.querySelectorAll('#saves-table tbody tr').length === 8", timeout=30)
        self.assertEqual(self.page.eval("document.querySelectorAll('#saves-table thead th').length"), 5)                     # (no column for save states: the app never copies them)
        self.assertIn("copies a save to the version you have", self.page.eval("document.getElementById('saves-now').textContent"))
        self.assertIn("PCSX2", self.page.eval("document.getElementById('saves-table').textContent"))
        self.page.eval("(() => { const s = document.getElementById('set-saved-games'); s.value = 'keep'; s.dispatchEvent(new Event('change')); })()")
        deadline = time.time() + 20
        while time.time() < deadline and self.api("GET", "/api/library/defaults")["profile"]["saved_games"] != "keep":
            time.sleep(0.1)
        self.assertEqual(self.api("GET", "/api/library/defaults")["profile"]["saved_games"], "keep")
        self.page.wait("document.getElementById('saves-now').textContent.includes('does not copy saves')", timeout=30)
        self.open("#/settings")                                                                        # (Settings no longer has it)
        self.page.wait("!!document.querySelector('#set-library-rules #rules-options')", timeout=30)
        self.assertFalse(self.page.eval("!!document.querySelector('#view-settings #set-saves-group')"))
        self.no_js_errors()

    def test_the_rules_wording_the_saves_of_a_file_the_filter_and_the_per_game_choice(self) -> None:
        self.retroarch_with_saves()
        self.api("POST", "/api/library/profile", {"platform": "Commodore Amiga", "prefer_saved": False})   # (else v1.0 is kept instead of v1.1)
        self.open()
        self.scan()
        self.settings_of()
        self.page.wait("!!document.getElementById('lib-saved-games')")
        self.assertEqual(self.page.eval("document.getElementById('lib-saved-games').value"), "convert")
        self.assertEqual(self.page.eval("[...document.getElementById('lib-saved-games').options].map(o => o.textContent)"),
                         ["Copy or convert the save to the latest version, leave the existing save (default)", "Keep both ROMs", "Archive the saves with the ROM",
                          "Leave the saves where they are", "Copy the save to the better version"])
        self.assertIn("When a rule replaces or archives a game you have saves for", self.page.eval("document.getElementById('lib-saves-group').textContent"))
        self.library_open()
        self.pick("show", "saves")                                                       # only the files of games that have saves
        self.open_file("Alpha Quest v1.0")
        cell = "document.querySelector('#files-tree .tree-detail:not(.hidden) .saves-cell')"
        self.page.wait(f"!!{cell}", timeout=30)
        text = self.page.eval(f"{cell}.textContent")
        self.assertIn("1 save \u00b7 1 state", text)
        self.assertIn("ROM kept for them", text)
        self.shot("file")
        # the choice for this one game: archive its saves with it
        self.assertEqual(self.page.eval(f"{cell}.querySelector('select.saves-choice').value"), "")
        self.page.eval(f"""(() => {{ const s = {cell}.querySelector('select.saves-choice'); s.value = 'archive'; s.dispatchEvent(new Event('change')); }})()""")
        self.page.wait("[...document.querySelectorAll('.toast')].some(t => t.textContent.includes('the list is updating'))")
        self.assertEqual(self.api("GET", "/api/library/profile?platform=Commodore%20Amiga")["profile"]["saved_overrides"],
                         [["Commodore Amiga - Games - [ADF]", "Alpha Quest v1.0 (1990)(Acme).adf", "archive"]])
        self.page.wait("document.getElementById('lib-live').textContent.includes('up to date') && !document.getElementById('files-tree').classList.contains('tree-busy')", timeout=60)
        plan = self.api("POST", "/api/library/plan", {"limit": 500})
        row = [r for r in plan["items"] if r.get("from", "").endswith("Alpha Quest v1.0 (1990)(Acme).adf")][0]
        self.assertEqual(row["saves"]["effect"], "archive")                              # the build now archives its saves with it
        self.settings_of()
        self.page.wait("!!document.getElementById('lib-saved-games')")
        self.select("lib-saved-games", "leave")
        self.page.wait("document.getElementById('lib-saved-games-note').textContent.includes('its saves are not touched')")
        self.assertEqual(self.api("GET", "/api/library/profile?platform=Commodore%20Amiga")["profile"]["saved_games"], "leave")
        self.assertIn("saves left alone", self.page.eval("document.getElementById('library-rules-summary').textContent"))
        self.no_js_errors()

    def test_the_saves_list_of_a_console_names_the_title_of_each(self) -> None:
        self.retroarch_with_saves()
        self.open()
        self.scan(platform="Nintendo Game Boy Advance", root=str(self.fx.gba))
        self.files_ready("nintendo-game-boy-advance")
        self.page.eval("document.getElementById('saves-box').open = true")
        self.page.wait("document.querySelectorAll('#saves-table tbody tr:not(.detail-row)').length === 3", timeout=30)
        text = self.page.eval("document.getElementById('saves-table').textContent")
        self.assertIn("ROM here", text)
        self.assertIn("title, no ROM here", text)
        self.assertIn("Alpha Run", text)
        self.shot("saves")
        self.no_js_errors()

    def test_the_retroarch_switch_is_the_same_setting_as_the_one_in_the_rules(self) -> None:
        self.retroarch_with_saves()
        self.open()
        self.scan()
        self.settings_of()
        self.page.wait("!!document.getElementById('lib-saves-follow')")
        self.assertTrue(self.page.eval("document.getElementById('lib-saves-follow').checked"))
        self.page.eval("document.getElementById('lib-saves-follow').click()")
        self.page.wait("!document.getElementById('lib-saves-follow').checked")
        for _ in range(50):                                                              # (the click saves in the background)
            if not self.api("GET", "/api/retroarch")["follow"]:
                break
            time.sleep(0.1)
        self.assertFalse(self.api("GET", "/api/retroarch")["follow"])
        self.page.goto(self.fx.url + "#/retroarch")                                        # (the switch is in the rules only: not on the Emulators page)
        self.page.wait("!document.getElementById('view-retroarch').classList.contains('hidden')")
        self.assertFalse(self.page.eval("!!document.getElementById('ra-follow')"))
        self.settings_of()
        self.page.wait("document.getElementById('lib-saves-follow').checked === false", timeout=10)
        self.no_js_errors()


class TidyFolderTests(UiTestCase):
    def confirm_dialog(self) -> str:
        self.page.wait("!document.getElementById('confirm-modal').classList.contains('hidden')")
        text = self.page.eval("document.getElementById('confirm-body').innerText")
        self.page.eval("document.getElementById('confirm-yes').click()")
        return text

    def test_build_in_place_can_move_the_set_aside_files_out_and_shows_time_and_data(self) -> None:
        aside = Path(tempfile.mkdtemp(prefix="romorg-ui-aside-"))
        self.addCleanup(shutil.rmtree, aside, True)
        self.open()
        self.assertFalse(self.page.eval("document.getElementById('lib-aside-none').classList.contains('hidden')"))   # no archive folder yet
        self.assertTrue(self.page.eval("document.getElementById('lib-aside-opts').classList.contains('hidden')"))     # so no archiving
        self.set_archive(str(aside.parent / "common-archive"))
        self.library_preview()
        self.settings_of()
        self.assertTrue(self.page.eval("document.getElementById('lib-aside-on').checked"))             # archiving is the default once there is one
        self.assertEqual(self.page.eval("document.getElementById('lib-aside-dir').value"), str(aside.parent / "common-archive"))
        listed = self.page.eval("[...document.querySelectorAll('#lib-aside-where li code')].map(c => c.textContent)")
        self.assertEqual(listed, ["_excluded", "_superseded", "_incomplete", "_duplicates", "_unmatched"])
        self.assertIn(f"common-archive/{self.fx.root.name}/", self.page.eval("document.querySelector('#lib-aside-where .archive-head code').textContent"))
        self.page.eval(f"""(() => {{ const i = document.getElementById('lib-aside-dir'); i.value = {str(aside)!r};
            i.dispatchEvent(new Event('change')); }})()""")
        self.page.wait("!!document.getElementById('lib-aside-reset')", timeout=30)                       # now this system's own folder
        self.page.eval("location.hash = '#/system/commodore-amiga/files'")
        self.page.wait("document.getElementById('lib-cards').textContent.includes('To move out to the archive folder')", timeout=60)
        self.page.wait("document.getElementById('lib-live')?.textContent.includes('up to date')", timeout=60)
        self.side("archive")
        self.assertIn(str(aside), self.page.eval("document.getElementById('files-where').textContent"))      # the archive side shows this system's own folder
        self.click("#lib-apply-btn")
        self.assertIn("is then moved out of this folder", self.confirm_dialog())
        self.page.wait("[...document.querySelectorAll('.toast')].some(t => t.textContent.includes('moved out to'))", timeout=60)
        self.assertFalse([p for p in self.fx.root.glob("_*")])
        self.assertTrue(any(aside.rglob("*.adf")))
        self.assertRegex(self.page.eval("document.querySelector('#job-bar .job-time')?.textContent || ''"), r"took \d+s|running")
        self.no_js_errors()


class RememberedViewTests(UiTestCase):
    def test_open_folders_stay_open_when_the_tree_is_drawn_again(self) -> None:
        self.library_preview()
        self.page.eval("document.querySelector('#files-tree button.tree-dir').click()")
        self.page.wait("document.querySelectorAll('#files-tree .tree-kids:not(.hidden) .tree-row').length > 0", timeout=30)
        opened = self.page.eval("document.querySelector('#files-tree button.tree-dir[aria-expanded=true]').dataset.treePath")
        self.pick("rated", "0") if self.page.eval("!!document.querySelector('#files-chips [data-filter=rated]')") else self.side("archive") or self.side("library")
        self.page.wait(f"[...document.querySelectorAll('#files-tree button.tree-dir[aria-expanded=true]')].some(b => b.dataset.treePath === {opened!r}) "
                       "|| !document.querySelector('#files-tree button.tree-dir')", timeout=30)
        self.no_js_errors()

    def test_compact_view_toggle_is_remembered(self) -> None:
        self.open()
        self.click("#density-btn")
        self.page.wait("document.body.classList.contains('dense')")
        self.page.goto(self.fx.url)
        self.page.wait("document.body.classList.contains('dense')")
        self.no_js_errors()


class ConvertDropdownTests(UiTestCase):
    """The global CHD settings page (writer / compression) and the Library option that converts raw discs to CHD first, with
    the built-in writer (a one-track synthetic PlayStation disc, no chdman, no audio so no libFLAC is needed)."""

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

    def tools(self) -> None:
        self.open("#/chd")
        self.page.wait("!document.getElementById('view-chd').classList.contains('hidden')")
        self.page.wait("document.getElementById('chdman-line').textContent.length > 5")

    def select(self, selector: str, value: str) -> None:
        self.page.eval(f"""(() => {{ const s = document.querySelector({selector!r}); s.value = {value!r};
            s.dispatchEvent(new Event('change', {{bubbles: true}})); }})()""")

    def confirm_dialog(self) -> str:
        self.page.wait("!document.getElementById('confirm-modal').classList.contains('hidden')")
        text = self.page.eval("document.getElementById('confirm-body').innerText")
        self.page.eval("document.getElementById('confirm-yes').click()")
        return text

    def idle(self) -> None:
        self.page.wait("(async () => { const j = await fetch('/api/job').then(r => r.json()); "
                       "return !j || j.status !== 'running'; })()", timeout=120)

    def test_writer_and_compression_dropdowns_are_saved_and_explained(self) -> None:
        from romorg import chdwrite
        self.tools()
        self.assertEqual(self.page.eval("[document.getElementById('chd-writer').value, document.getElementById('chd-preset').value]"),
                         ["auto", "default"])
        self.assertEqual(self.page.eval("document.getElementById('chd-preset-note').classList.contains('hidden')"), True)
        zstd_option = "document.querySelector('#chd-preset option[value=zstd]').disabled"
        if chdwrite.zstd_available():
            self.assertFalse(self.page.eval(zstd_option))
            self.select("#chd-preset", "zstd")
            self.page.wait("!document.getElementById('chd-preset-note').classList.contains('hidden')")
            self.assertIn("older emulators", self.page.eval("document.getElementById('chd-preset-note').textContent"))
            self.page.goto(self.fx.url + "#/chd")                              # saved on the server: survives a reload
            self.page.eval("location.reload()")
            self.page.wait("document.getElementById('chdman-line').textContent.length > 5")
            self.page.wait("document.getElementById('chd-preset').value === 'zstd'")
        else:
            self.assertTrue(self.page.eval(zstd_option))                       # no Zstandard library: not offered
        self.select("#chd-writer", "chdman")
        self.page.wait("document.getElementById('chd-writer').value === 'chdman'")
        self.page.wait(f"{zstd_option} === true")
        self.assertTrue(self.page.eval(zstd_option))                            # chdman writes no Zstandard
        self.assertEqual(self.page.eval("document.getElementById('chd-preset').value"), "default")
        self.select("#chd-writer", "auto")
        self.page.wait("document.getElementById('chd-writer').value === 'auto'")
        self.no_js_errors()

    def test_the_settings_page_has_the_scan_verification_switch(self) -> None:
        self.tools()
        self.assertFalse(self.page.eval("document.getElementById('chd-verify-scan').checked"))
        self.page.eval("document.getElementById('chd-verify-scan').click()")
        self.page.wait("[...document.querySelectorAll('.toast')].some(t => t.textContent.includes('Saved'))")
        self.page.eval("location.reload()")
        self.page.wait("document.getElementById('chdman-line').textContent.length > 5")
        self.page.wait("document.getElementById('chd-verify-scan').checked")
        self.assertFalse(self.page.eval("!!document.getElementById('tool-convert') || !!document.getElementById('tool-verify')"))
        self.no_js_errors()

    def api(self, method: str, path: str, body: dict | None = None) -> dict:
        import json
        import urllib.request
        req = urllib.request.Request(self.fx.url.rstrip("/") + path, method=method, data=json.dumps(body or {}).encode() if method == "POST" else None,
                                     headers={"X-Romorg-Token": "t", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())

    def test_the_library_converts_raw_discs_when_building(self) -> None:
        self.open()
        self.scan(self.PLATFORM, str(self.psx))
        self.page.goto(self.fx.url + "#/settings")                                       # the rule "Convert when building": your default for every system
        box = "#set-library-rules input[data-opt=\"%s\"]"
        self.page.wait(f"!!document.querySelector({box % 'delete_converted'!r})", timeout=30)
        self.assertTrue(self.page.eval(f"document.querySelector({box % 'convert_first'!r}).checked"))    # (both on by default)
        self.assertTrue(self.page.eval(f"document.querySelector({box % 'delete_converted'!r}).checked"))
        self.click(box % "delete_converted")                                              # off: the originals are kept
        deadline = time.time() + 20
        while time.time() < deadline and self.api("GET", "/api/library/defaults")["profile"]["delete_converted"]:
            time.sleep(0.1)
        profile = self.api("GET", "/api/library/defaults")["profile"]
        self.assertEqual((profile["convert_first"], profile["delete_converted"]), (True, False))
        self.assertFalse(self.page.eval("!!document.getElementById('lib-convert')"))               # (no box of its own on the system's page any more)
        self.page.goto(self.fx.url + f"#/system/{self.SLUG}/files")
        self.page.wait("document.getElementById('lib-cards').textContent.includes('Raw discs to convert to CHD first')", timeout=60)
        self.click("#lib-apply-btn")
        said = self.confirm_dialog()
        self.assertIn("converted to CHD first", said)
        self.assertIn("kept in _converted_originals", said)
        self.page.wait("[...document.querySelectorAll('.toast')].some(t => t.textContent.includes('converted first'))", timeout=180)
        self.idle()
        chd = self.psx / "Solo (USA)" / "Solo (USA).chd"
        self.assertTrue(chd.is_file())
        self.assertTrue((self.psx / "_converted_originals" / "raw" / "t.bin").is_file())
        self.assertFalse((self.psx / "raw" / "t.bin").exists())
        self.no_js_errors()


# Runs in the page before its own script: answers some API calls itself (``rules``) and notes every call in
# ``window.__calls``, so a test can show the page a Windows server, a server without a folder dialog, or a failing one.
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
      if (r.delay) { await new Promise((res) => setTimeout(res, r.delay)); if (!r.patch && !r.pick && r.body === undefined) return real(url, opts); }
      if (r.patch) { const res = await real(url, opts); return json(res.status, Object.assign(await res.json(), r.patch)); }
      if (r.pick) return json(200, { path: r.pick + ((sent || {}).title || '') });
      return json(r.status || 200, r.body);
    }
    return real(url, opts);
  };
})()"""

# every "Browse..." button of the UI (the native folder dialog) and the field its answer belongs in
FOLDER_FIELDS = {          # "Folders..." button -> the field it fills (the in-app folder browser, the same on every platform)
    "folder-browse-btn": "folder-input", "lib-export-browse-btn": "lib-export-dest", "lib-aside-browse": "lib-aside-dir",
    "ra-custom-browse": "ra-custom", "ra-save-browse": "ra-save-dir", "ra-state-browse": "ra-state-dir",
    "ra-backup-browse": "ra-backup-dir", "ra-bios-browse": "ra-bios-search",
    "set-archive-browse": "set-archive-dir",
    **{f"emu-{k}-browse": f"emu-{k}-folder" for k in ("dolphin", "cemu", "pcsx2", "eden", "ryujinx")},
}
LINUX_ONLY = r"~/|/home/deck|/run/media|Steam Deck|Discover|[Ff]latpak|fusermount|/path/to"


class PlatformTests(UiTestCase):
    """What differs between a Windows server and a Linux one, and what must not: wording, example paths, the folder
    pickers, the layout of a narrow window. The page learns the platform from ``/api/status`` (``os``)."""

    def stub(self, *rules: dict) -> None:
        import json
        self.page.call("Page.addScriptToEvaluateOnNewDocument", source=_FETCH_STUB % json.dumps(list(rules)))

    def show(self, hash_: str) -> None:
        self.page.eval(f"location.hash = {hash_!r}")
        self.page.wait(f"location.hash === {hash_!r}")
        time.sleep(0.5)

    def test_while_the_list_is_worked_out_the_header_says_so_instead_of_offering_a_scan(self) -> None:
        self.stub({"path": "/api/library/plan", "method": "POST", "delay": 2500})
        self.open()
        self.scan()
        self.page.goto(self.fx.url + "#/system/commodore-amiga/files")
        self.page.wait("!document.getElementById('sys-busy').classList.contains('hidden')", timeout=30)
        self.assertTrue(self.page.eval("document.getElementById('scan-btn').classList.contains('busy-hidden')"))
        self.assertIn("the list", self.page.eval("document.getElementById('sys-busy').textContent"))
        self.page.wait("document.getElementById('lib-live').textContent.includes('up to date')", timeout=60)
        self.assertTrue(self.page.eval("document.getElementById('sys-busy').classList.contains('hidden')"))
        self.assertFalse(self.page.eval("document.getElementById('scan-btn').classList.contains('busy-hidden')"))
        self.no_js_errors()

    def test_while_the_last_scan_is_read_back_the_header_says_so_instead_of_scan_folder(self) -> None:
        self.stub({"path": "/api/scan/select", "method": "POST", "delay": 2500})
        self.open()
        self.scan()
        self.scan(platform="Nintendo Game Boy Advance", root=str(self.fx.gba))           # (the Amiga's scan is no longer the current one)
        self.page.eval("location.reload()")
        self.page.wait("document.querySelectorAll('.nav-item[data-platform]').length > 3")
        self.page.goto(self.fx.url + "#/system/commodore-amiga/files")
        self.page.wait("document.getElementById('sys-busy-text').textContent.includes('Loading the last scan')", timeout=30)
        self.assertTrue(self.page.eval("document.getElementById('scan-btn').classList.contains('busy-hidden')"))
        self.assertIn("Loading the last scan", self.page.eval("document.getElementById('lib-gate').textContent"))
        # from the end of the load to the end of the list the header stays busy: the scan button never shows in between
        self.page.eval("""window.__seen = []; (function look() { const b = document.getElementById('scan-btn');
          window.__seen.push(b.classList.contains('busy-hidden') ? 'busy' : b.textContent); if (window.__seen.length < 400) setTimeout(look, 25); })()""")
        self.page.wait("document.getElementById('lib-live').textContent.includes('up to date')", timeout=60)
        self.page.wait("!document.getElementById('scan-btn').classList.contains('busy-hidden')", timeout=10)
        seen = self.page.eval("window.__seen")
        first_button = next((i for i, v in enumerate(seen) if v != "busy"), len(seen))
        self.assertTrue(all(v != "busy" for v in seen[first_button:]), seen)               # (busy, then the button for good: no flicker)
        self.assertEqual(self.page.eval("document.getElementById('scan-btn').textContent"), "Rescan folder")
        self.page.eval("location.hash = '#/system/commodore-amiga/settings'")                # (on the Settings tab the header has no list to wait for)
        self.page.wait("!document.getElementById('scan-btn').classList.contains('busy-hidden')", timeout=10)
        self.no_js_errors()

    def test_a_windows_server_shows_windows_paths_and_no_linux_wording(self) -> None:
        self.stub({"path": "/api/status", "patch": {"os": "windows"}},
                  {"path": "/api/chdman", "patch": {"os": "windows"}})
        self.open("#/retroarch")
        self.page.wait("document.querySelectorAll('.nav-item[data-platform]').length > 3")
        found = []
        for view in ("#/settings", "#/retroarch", "#/chd", "#/system/commodore-amiga/settings",
                     "#/system/commodore-amiga/files", "#/system/sony-playstation/settings"):
            self.show(view)
            found += self.page.eval(r"""(() => { const bad = new RegExp(%r), out = [];
              for (const e of document.querySelectorAll('input[placeholder]')) if (bad.test(e.placeholder)) out.push(e.id + ': ' + e.placeholder);
              for (const e of document.querySelectorAll('[title]')) if (bad.test(e.title)) out.push('title: ' + e.title);
              const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
              while (w.nextNode()) {                         // (a folder found on this machine is data shown, not wording: left out)
                if (w.currentNode.parentElement && w.currentNode.parentElement.closest('.emu-facts')) continue;
                if (bad.test(w.currentNode.textContent)) out.push(w.currentNode.textContent.trim().slice(0, 120));
              }
              return out; })()""" % LINUX_ONLY)
        self.assertEqual(sorted(set(found)), [])
        self.assertIn("D:\\Emulation\\roms\\psx", self.page.eval("document.getElementById('folder-input').placeholder"))
        self.no_js_errors()

    def test_a_linux_server_keeps_its_own_example_paths(self) -> None:
        self.stub({"path": "/api/status", "patch": {"os": "linux"}}, {"path": "/api/chdman", "patch": {"os": "linux"}})
        self.open()
        self.page.wait("document.querySelectorAll('.nav-item[data-platform]').length > 3")
        self.show("#/system/commodore-amiga/settings")
        self.assertEqual(self.page.eval("document.getElementById('folder-input').placeholder"), "/run/media/deck/<SD>/roms/amiga")
        self.show("#/chd")
        self.page.wait("document.getElementById('chdman-path').placeholder.includes('chdman')")
        self.assertIn("/path/to/chdman", self.page.eval("document.getElementById('chdman-path').placeholder"))
        self.no_js_errors()

    def test_every_folder_field_has_the_one_folder_browser_and_there_is_no_native_dialog(self) -> None:
        """One way to choose a folder, the same on Windows and Linux: the app's own "Folders..." browser. (A native dialog
        used to be offered as "Browse..." where the server had one; on Windows it opened behind the browser, so it looked
        as if nothing happened.)"""
        ra = self.fx.tmp / "ra"
        ra.mkdir()
        (ra / "retroarch.cfg").write_text('savefile_directory = "default"\n')
        self.stub({"path": "/api/folders", "method": "POST", "body": {"folders": {}}})
        self.open("#/retroarch")
        self.page.wait("!document.getElementById('view-retroarch').classList.contains('hidden')")
        self.page.wait("document.querySelectorAll('.nav-item[data-platform]').length > 3")
        self.page.eval(f"(() => {{ const i = document.getElementById('ra-custom'); i.value = {str(ra)!r}; }})()")
        self.click("#ra-custom-add")
        self.page.wait("!document.getElementById('ra-change-panel').classList.contains('hidden')")
        self.show("#/settings")
        self.show("#/system/commodore-amiga/settings")
        self.assertEqual(self.page.eval("[...document.querySelectorAll('button')].filter(b => b.textContent.trim() === 'Browse...').length"), 0)
        self.assertEqual(self.page.eval("document.querySelectorAll('button[id*=\"-native\"]').length"), 0)
        ids = self.page.eval("[...document.querySelectorAll('button')].filter(b => b.textContent.trim() === 'Folders...').map(b => b.id).sort()")
        self.assertEqual(ids, sorted(FOLDER_FIELDS))                       # every folder field has it, and nothing else does
        for button in FOLDER_FIELDS:
            self.assertFalse(self.page.eval(f"document.getElementById({button!r}).classList.contains('hidden')"), button)
        # pressing one opens the in-app browser (a modal), for that field
        self.show("#/settings")
        self.page.eval("document.getElementById('set-archive-browse').click()")
        self.page.wait("!document.getElementById('folder-modal').classList.contains('hidden')")
        self.no_js_errors()

    def test_a_narrow_window_fits_every_page_and_the_system_list_opens_and_closes(self) -> None:
        self.open()
        self.page.wait("document.querySelectorAll('.nav-item[data-platform]').length > 3")
        self.scan()
        for width in (700, 420):
            self.page.call("Emulation.setDeviceMetricsOverride", width=width, height=900, deviceScaleFactor=1, mobile=False)
            for view in ("#/", "#/settings", "#/retroarch", "#/chd", "#/system/commodore-amiga/files",
                         "#/system/commodore-amiga/settings", "#/system/commodore-amiga/files?show=missing"):
                with self.subTest(width=width, view=view):
                    self.show(view)
                    over = self.page.eval("document.documentElement.scrollWidth - document.documentElement.clientWidth")
                    self.assertLessEqual(over, 0, "the page is wider than the window")
        self.show("#/settings")                                         # a page is open: the list is folded away
        self.assertEqual(self.page.eval("document.getElementById('shell').dataset.side"), "closed")
        self.click("#side-toggle")
        self.page.wait("document.getElementById('shell').dataset.side === 'open'")
        self.assertEqual(self.page.eval("document.querySelectorAll('.nav-item[data-platform]').length"), 20)
        last = "[...document.querySelectorAll('.nav-item[data-platform]')].pop()"
        self.assertTrue(self.page.eval(f"(() => {{ const r = {last}.getBoundingClientRect(); return r.width > 100 && r.right <= innerWidth; }})()"))
        self.page.eval(f"{last}.click()")                                # picking a system closes the list again
        self.page.wait("document.getElementById('shell').dataset.side === 'closed' && location.hash.startsWith('#/system/')")
        self.page.call("Emulation.clearDeviceMetricsOverride")
        self.no_js_errors()


class RobustnessTests(UiTestCase):
    """Double clicks, failing answers and hostile names: a message, never an exception, never markup."""

    stub = PlatformTests.stub

    def test_a_failing_server_gives_messages_not_exceptions(self) -> None:
        self.stub({"path": "/api/library/defaults", "status": 500, "body": {"error": "RuntimeError: boom"}},
                  {"path": "/api/retroarch", "status": 500, "body": {"error": "RuntimeError: bang"}},
                  {"path": "/api/chdman", "status": 500, "body": {"error": "RuntimeError: crash"}})
        self.open("#/settings")
        self.page.wait("document.getElementById('set-library-rules').textContent.includes('boom')")
        self.page.eval("location.hash = '#/retroarch'")
        self.page.wait("[...document.querySelectorAll('#toasts .toast')].some(t => t.textContent.includes('bang'))")
        self.page.eval("document.getElementById('ra-custom-add').click()")
        self.page.wait("[...document.querySelectorAll('#toasts .toast')].some(t => t.textContent.includes('retroarch.cfg'))")
        self.page.eval("location.hash = '#/chd'")
        time.sleep(0.5)
        self.no_js_errors()

    def test_names_from_a_dat_are_shown_as_text_never_as_markup(self) -> None:
        import hashlib
        import zlib
        from uifixture import GBA_DAT
        evil = '<img src=x onerror=window.__xss=1><b id=xss-b>bold</b> & <script>window.__xss=2</script>'
        data = b"evil rom " * 500
        (self.fx.data / "nointro" / f"{GBA_DAT}.dat").write_text(
            f'clrmamepro (\n\tname "{GBA_DAT}"\n\tdescription "{GBA_DAT}"\n\tversion "20250101-000000"\n)\n'
            f'game (\n\tname "{evil} (USA)"\n\tdescription "{evil} (USA)"\n\trom ( name "{evil} (USA).gba" size {len(data)} '
            f'crc {zlib.crc32(data):08x} md5 {hashlib.md5(data).hexdigest()} sha1 {hashlib.sha1(data).hexdigest()} )\n)\n')
        odd = self.fx.gba / ("it's odd & co; x=1" if os.name == "nt" else "it's \"odd\" & <i>co</i>")
        odd.mkdir(parents=True)
        (odd / "dump.gba").write_bytes(data)
        self.open()
        self.scan("Nintendo Game Boy Advance", str(self.fx.gba))
        self.files_ready("nintendo-game-boy-advance")
        self.open_file("onerror")                 # (the new file name is made safe for the file system; the details carry the DAT's name)
        detail = "#files-tree .tree-detail:not(.hidden)"
        self.page.wait(f"!!document.querySelector('{detail} .cs-sums table')", timeout=30)
        self.page.wait(f"document.querySelector('{detail}').innerText.includes('<img src=x onerror')")
        # a name and a checksum can be copied with one press (to search for the file)
        self.assertTrue(self.page.eval(f"!!document.querySelector('{detail} .copy-chip.name')"))
        self.page.eval(f"document.querySelector('{detail} .copy-chip.name').click()")
        self.page.wait("[...document.querySelectorAll('.toast')].some(t => t.textContent.includes('Copied'))")
        for view in ("files?show=missing", "files?show=unmatched", "files?show=have", "settings"):
            self.page.goto(self.fx.url + "#/system/nintendo-game-boy-advance/" + view)
            self.page.wait("!document.getElementById('view-system').classList.contains('hidden')")
            time.sleep(0.5)
        self.assertIsNone(self.page.eval("window.__xss === undefined ? null : window.__xss"))
        self.assertEqual(self.page.eval("document.querySelectorAll('img, #xss-b, main script').length"), 0)
        self.no_js_errors()


class RememberedScanTests(UiTestCase):
    """Going to another system and back needs no new scan; the Scan button becomes Rescan, with a way to recalculate everything."""

    def test_back_to_a_scanned_system_shows_its_scan_and_the_buttons_say_rescan(self) -> None:
        self.open()
        self.scan()
        self.scan(platform="Nintendo Game Boy Advance", root=str(self.fx.gba))
        self.page.eval("location.reload()")
        self.page.goto(self.fx.url + "#/system/commodore-amiga/settings")
        self.page.wait("!!document.querySelector('#summary-cards .card')", timeout=30)                 # (restored: nobody pressed Scan)
        self.page.wait("document.getElementById('scan-btn').textContent === 'Rescan folder'", timeout=30)
        self.assertFalse(self.page.eval("document.getElementById('scan-force-btn').classList.contains('hidden')"))
        self.assertEqual(self.page.eval("""fetch('/api/status').then(r => r.json()).then(s => s.scan && s.scan.platform)"""), "Commodore Amiga")
        self.page.goto(self.fx.url + "#/system/nintendo-game-boy-advance/settings")
        self.page.wait("!!document.querySelector('#summary-cards .card') && document.getElementById('sys-title').textContent.includes('Game Boy Advance')", timeout=30)
        self.page.goto(self.fx.url + "#/system/commodore-amiga/settings")
        self.page.wait("document.getElementById('sys-title').textContent.includes('Amiga') && !!document.querySelector('#summary-cards .card')", timeout=30)
        self.assertIn("Amiga", self.page.eval("document.getElementById('sys-title').textContent"))
        self.page.goto(self.fx.url + "#/system/sega-mega-drive-genesis/settings")                     # never scanned
        self.page.wait("document.getElementById('scan-btn').textContent === 'Scan folder'", timeout=30)
        self.assertTrue(self.page.eval("document.getElementById('scan-force-btn').classList.contains('hidden')"))
        self.no_js_errors()


class SettingsTests(UiTestCase):
    """The archive folder is set once for every system (Settings); a system can have its own on the Library tab."""

    def test_the_global_archive_folder_reaches_the_library_tab_and_a_system_can_override_it(self) -> None:
        base = Path(tempfile.mkdtemp(prefix="romorg-ui-set-"))
        self.addCleanup(shutil.rmtree, base, True)
        self.open()
        self.scan()
        self.page.goto(self.fx.url + "#/settings")
        self.page.wait("!document.getElementById('view-settings').classList.contains('hidden')")
        self.assertEqual(self.page.eval("document.getElementById('set-archive-dir').value"), "")
        self.assertIn("nothing is archived", self.page.eval("document.getElementById('set-archive-default').textContent"))   # no default
        self.page.eval(f"""(() => {{ const i = document.getElementById('set-archive-dir'); i.value = {str(base / "everything")!r};
            i.dispatchEvent(new Event('change')); }})()""")
        self.page.wait("document.getElementById('set-archive-default').textContent.includes('unless it has its own')", timeout=30)
        self.page.goto(self.fx.url + "#/system/commodore-amiga/settings")
        self.page.wait(f"document.getElementById('lib-aside-dir').value === {str(base / 'everything')!r}", timeout=30)
        self.assertTrue(self.page.eval("document.getElementById('lib-aside-none').classList.contains('hidden')"))
        self.assertFalse(self.page.eval("!!document.getElementById('lib-aside-reset')"))
        self.page.eval(f"""(() => {{ const i = document.getElementById('lib-aside-dir'); i.value = {str(base / "amiga-only")!r};
            i.dispatchEvent(new Event('change')); }})()""")
        self.page.wait("!!document.getElementById('lib-aside-reset')", timeout=30)
        self.page.goto(self.fx.url + "#/settings")
        self.page.wait("document.querySelectorAll('#set-archive-own li').length === 1", timeout=30)
        self.assertIn("Commodore Amiga", self.page.eval("document.getElementById('set-archive-own').textContent"))
        self.page.eval("document.querySelector('#set-archive-own button').click()")
        self.page.wait("document.querySelectorAll('#set-archive-own li').length === 0", timeout=30)
        self.page.goto(self.fx.url + "#/system/commodore-amiga/settings")
        self.page.wait(f"document.getElementById('lib-aside-dir').value === {str(base / 'everything')!r}", timeout=30)
        self.no_js_errors()

    def test_the_library_tab_no_longer_describes_folders_next_to_the_games(self) -> None:
        self.open()
        self.scan()
        self.files_ready()
        self.assertNotIn("next to the games", self.page.eval("document.getElementById('view-system').textContent"))
        self.assertFalse(self.page.eval("!!document.getElementById('library-intro')"))
        self.no_js_errors()


class S3ArchiveUiTests(UiTestCase):
    """The S3 connection in Settings (the secret never shown again), its test, and the Archive side of the file tree as the bucket."""

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

    def test_the_connection_is_set_up_tested_and_the_archive_tree_is_the_bucket(self) -> None:
        self.open("#/settings")
        self.page.wait("!!document.getElementById('s3-save')?.offsetParent", timeout=30)
        self.fill("s3-endpoint", f"http://127.0.0.1:{self.s3srv.server_address[1]}")
        self.fill("s3-access", self.ak)
        self.fill("s3-secret", self.sk)
        self.click("#s3-save")
        self.page.wait("document.getElementById('s3-secret').placeholder.includes('stored')")
        self.assertEqual(self.page.eval("document.getElementById('s3-secret').value"), "")
        self.set_archive("s3://bucket/arch")
        self.page.goto(self.fx.url + "#/settings")
        self.page.wait("document.getElementById('set-archive-default').textContent.includes('S3 bucket')", timeout=30)
        self.assertEqual(self.page.eval("document.getElementById('set-archive-dir').value"), "s3://bucket/arch/")
        self.page.wait("!document.getElementById('s3-test').disabled")
        self.click("#s3-test")
        self.page.wait("document.getElementById('s3-status').textContent.startsWith('Works')", timeout=30)
        self.assertNotIn(self.sk, self.page.eval("document.documentElement.outerHTML"))
        self.scan()
        self.files_ready()
        self.click("#files-sides button[data-side='archive']")
        self.page.wait("document.getElementById('files-where').textContent.includes('s3://bucket/arch/')", timeout=30)
        self.assertTrue(self.page.eval("document.querySelector('#files-where .badge')?.textContent === 'S3'"))
        self.page.wait("document.querySelectorAll('#files-tree .tree-row').length > 0", timeout=30)
        self.assertIn("Uploaded", self.page.eval("document.getElementById('files-chips').textContent"))
        self.no_js_errors()


class RestoreUiTests(UiTestCase):
    """Ticking files and folders on the Archive side and restoring them (the archive keeps its copy)."""

    def test_chosen_files_are_restored_from_the_archive(self) -> None:
        arch = Path(tempfile.mkdtemp(prefix="romorg-ui-arch-"))
        self.addCleanup(shutil.rmtree, arch, True)
        mine = arch / self.fx.root.name / "_unmatched" / "notes"
        mine.mkdir(parents=True)
        (mine / "readme.txt").write_bytes(b"my notes")
        (mine / "todo.txt").write_bytes(b"later")
        self.open()
        self.set_archive(str(arch))
        self.scan()
        self.files_ready()
        self.click("#files-sides button[data-side='archive']")
        self.page.wait("!!document.querySelector('#files-tree input[data-restore-path=\"_unmatched\"]')", timeout=30)
        self.assertTrue(self.page.eval("document.getElementById('files-restore-bar').classList.contains('hidden')"))
        self.click("#files-tree input[data-restore-path='_unmatched']")
        self.page.wait("!document.getElementById('files-restore-bar').classList.contains('hidden')")
        self.assertIn("1 chosen", self.page.eval("document.getElementById('files-restore-count').textContent"))
        self.click("#files-restore-btn")
        self.page.wait("[...document.querySelectorAll('.toast')].some(t => t.textContent.includes('Restored 2'))", timeout=60)
        self.assertEqual((self.fx.root / "notes" / "readme.txt").read_bytes(), b"my notes")
        self.assertTrue((mine / "readme.txt").is_file())                               # the archive keeps its copy
        self.page.wait("document.getElementById('files-restore-bar').classList.contains('hidden')")
        self.no_js_errors()


class EmulatorsUiTests(NoMachineFolders, UiTestCase):
    """The Emulators page: one row per emulator, found folders selected, a switch to ignore one."""

    def test_found_folders_are_selected_and_an_emulator_can_be_switched_off(self) -> None:
        home = Path(os.environ["HOME"])
        (home / ".config" / "PCSX2" / "inis").mkdir(parents=True)
        (home / ".config" / "PCSX2" / "inis" / "PCSX2.ini").write_text("[Folders]\n")
        self.open("#/retroarch")
        self.page.wait("document.querySelectorAll('#emu-list .emu-card').length === 5", timeout=30)
        self.page.wait(f"document.getElementById('emu-pcsx2-folder').value === {str(home / '.config' / 'PCSX2')!r}", timeout=30)
        self.assertIn("found on this machine", self.page.eval("document.getElementById('emu-pcsx2-note').textContent"))
        self.assertIn("not found", self.page.eval("document.getElementById('emu-dolphin-note').textContent"))
        self.assertTrue(self.page.eval("document.getElementById('emu-pcsx2-box').classList.contains('hidden')"))   # (the folder is behind Change...)
        self.page.eval("document.getElementById('emu-pcsx2-change').click()")
        self.page.wait("!document.getElementById('emu-pcsx2-box').classList.contains('hidden')")
        self.page.eval("document.getElementById('emu-pcsx2-on').click()")
        self.page.wait("document.getElementById('emu-pcsx2-note').textContent.includes('switched off')", timeout=30)
        self.assertTrue(self.page.eval("document.getElementById('emu-pcsx2-folder').disabled"))
        self.assertIn("Nintendo Switch", self.page.eval("document.querySelector('[data-emulator=eden]').textContent"))
        self.no_js_errors()


class SwitchUiTests(NoMachineFolders, UiTestCase):
    """The Nintendo Switch as a system: games, updates and add-ons in one folder, matched by title ID and version, with the saves of the
    emulators and the same Library tab as any system."""

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
        name = f"{APP}{'0' * 16}.tik".encode() + b"\0"
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
        self.open()
        self.page.wait("document.querySelectorAll('.nav-item[data-platform]').length > 3")
        for key, folder in (("eden", base / "nand"),):
            self.page.eval(f"""(async()=>{{const t=document.querySelector('meta[name=romorg-token]').content;
 await fetch('/api/emulators/config',{{method:'POST',headers:{{'Content-Type':'application/json','X-Romorg-Token':t}},body:JSON.stringify({{source:{key!r},folder:{str(folder)!r}}})}});
 await fetch('/api/folders',{{method:'POST',headers:{{'Content-Type':'application/json','X-Romorg-Token':t}},body:JSON.stringify({{platform:'Nintendo Switch',path:{str(base / 'games')!r}}})}}) }})()""")
        time.sleep(0.5)
        self.assertEqual(self.page.eval("document.querySelector('[data-platform=\"Nintendo Switch\"]').closest('.side-group').querySelector('.side-title').textContent"), "Cartridge consoles")
        self.scan("Nintendo Switch", str(base / "games"))
        self.page.eval("location.hash = '#/system/nintendo-switch/settings'")
        self.page.wait("!document.getElementById('ov-saves').classList.contains('hidden')", timeout=60)
        self.assertIn("Saves (Eden)", self.page.eval("document.getElementById('ov-saves-title').textContent"))
        # the game and its update are two entries of the catalogue, both had; the file the database does not know is unmatched
        self.files_ready("nintendo-switch", "?show=have")
        self.open_folders()
        self.assertGreaterEqual(self.page.eval("document.querySelectorAll('#files-tree button.tree-file').length"), 2)
        self.assertIn("Some Game - Update", self.page.eval("document.getElementById('files-tree').textContent"))
        self.pick("show", "unmatched")
        self.open_folders()
        self.assertEqual(self.page.eval("document.querySelectorAll('#files-tree button.tree-file').length"), 1)
        # the rules are the same as any system's
        self.settings_of("nintendo-switch")
        self.assertTrue(self.page.eval("document.getElementById('library-rules-box') !== null"))
        self.no_js_errors()


class SidebarProgressTests(UiTestCase):
    """The bar under a system's name while it is scanned: it has to be visible and move, and it must not repeat the job bar."""

    def run_job(self, steps):
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

    def bar(self):
        return self.page.eval("""(() => { const it = document.querySelector('.nav-item.running');
            if (!it) return null; const fill = it.querySelector('.nav-job .progress-bar'), track = it.querySelector('.nav-job .progress');
            return {fill: fill.getBoundingClientRect().width, track: track.getBoundingClientRect().width,
                    indeterminate: track.classList.contains('indeterminate'), inline: fill.style.width,
                    text: it.querySelector('.nav-job').textContent.trim(), tip: it.title,
                    top: document.getElementById('job-bar').innerText}; })()""")

    def test_the_sidebar_bar_is_visible_and_moves_and_does_not_repeat_the_message(self) -> None:
        step = self.run_job(None)                       # already running when the page loads, as after a click and a reload
        self.open()
        self.page.wait("!!document.querySelector('.nav-item.running')", timeout=20)
        # the job has not counted its work yet: the bar slides instead of sitting at 0 px
        b = self.bar()
        self.assertTrue(b["indeterminate"], b)
        self.assertGreater(b["fill"], 5, b)
        self.assertEqual(b["inline"], "", b)
        # 0.1 % of a very long scan: still a visible sliver, not 0 px
        step(931, 931469)
        self.page.wait("document.querySelector('.nav-item.running .progress-bar').style.width !== ''", timeout=10)
        b = self.bar()
        self.assertFalse(b["indeterminate"], b)
        self.assertGreaterEqual(b["fill"], 0.019 * b["track"], b)
        # half way: half the track
        step(500, 1000)
        self.page.wait("""(() => { const f = document.querySelector('.nav-item.running .progress-bar'), t = f.parentElement;
            return Math.abs(f.getBoundingClientRect().width / t.getBoundingClientRect().width - 0.5) < 0.01; })()""", timeout=10)   # (it eases there)
        b = self.bar()
        self.assertAlmostEqual(b["fill"] / b["track"], 0.5, delta=0.02)
        # the words are the job bar's; the sidebar only has the bar (and the words as a tooltip)
        self.assertEqual(b["text"], "", b)
        self.assertIn("Reading a.chd (file 1/9)", b["tip"])
        self.assertIn("Reading a.chd (file 1/9)", b["top"])
        # a sliver is written with a decimal in the job bar, not as "0%"
        step(4, 1000)
        self.page.wait("document.querySelector('#job-bar .job-count')?.textContent.includes('0.4%')", timeout=10)
        self.no_js_errors()




class WhdloadInstallsTests(UiTestCase):
    """The Amiga's WHDLoad installs: which of your games has one (a list of installs kept in the data folder), filtered and searched."""

    def test_the_games_with_an_install_are_listed(self) -> None:
        import json
        import time
        folder = self.fx.data / "whdinstalls"
        folder.mkdir(parents=True, exist_ok=True)
        installs = [{"archive": a, "title": t, "size": 30000, "date": "2020-01-01", "hol": "", "lemon": ""}
                    for a, t in (("AlphaQuest.lha", "Alpha Quest"), ("BetaBlaster.lha", "Beta Blaster"), ("ZetaZone.lha", "Zeta Zone"))]
        (folder / "installs.json").write_text(json.dumps({"fetched": "2026-10-18", "fetched_at": time.time(), "installs": installs}))
        self.library_preview()
        self.page.wait("!document.getElementById('ov-whdload').classList.contains('hidden')", timeout=30)
        self.assertIn("3 of your 7 games have one", self.page.eval("document.getElementById('ov-whdload-title').textContent"))
        self.page.eval("document.getElementById('ov-whdload').open = true")
        rows = "[...document.querySelectorAll('#ov-whdload tbody tr')].map(r => r.cells[0].textContent)"
        self.assertEqual(self.page.eval(rows), ["Alpha Quest", "Beta Blaster", "Zeta Zone"])
        machines = "[...document.querySelectorAll('#ov-whdload tbody tr')].map(r => r.cells[2].textContent)"
        self.assertTrue(all(m.startswith(("A500", "A600", "A1200", "CD32")) for m in self.page.eval(machines)), self.page.eval(machines))
        self.page.eval("(() => { const s = document.getElementById('whd-search'); s.value = 'beta'; s.dispatchEvent(new Event('input')); })()")
        self.page.wait(f"{rows}.length === 1", timeout=10)
        self.page.eval("(() => { const s = document.getElementById('whd-search'); s.value = ''; s.dispatchEvent(new Event('input')); })()")
        self.page.wait(f"{rows}.length === 3", timeout=10)
        self.page.eval("(() => { const s = document.querySelector('#ov-whdload select'); s.value = 'none'; s.dispatchEvent(new Event('change')); })()")
        self.assertEqual(self.page.eval(rows), ["Delta Force", "Epsilon", "Eta Escape", "Gamma Run"])
        self.no_js_errors()

    def test_another_system_has_no_such_card(self) -> None:
        self.open()
        self.scan("Nintendo Game Boy Advance", str(self.fx.gba))
        self.files_ready("nintendo-game-boy-advance")
        self.assertTrue(self.page.eval("document.getElementById('ov-whdload').classList.contains('hidden')"))
        self.no_js_errors()
if __name__ == "__main__":
    unittest.main()
