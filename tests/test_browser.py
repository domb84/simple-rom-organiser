"""Real-browser checks of the UI (headless Chromium / Brave over the DevTools protocol).

Skipped unless a browser is available: set ROMORG_BROWSER_PORT to an already running ``--remote-debugging-port``, or have
Chromium / Chrome on the PATH, or (Steam Deck) the Brave flatpak. The app runs for real on a small synthetic Amiga library
(``tests/uifixture.py``); the checks click through the UI and fail on any JavaScript error."""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from cdp import Browser  # noqa: E402
from uifixture import Fixture  # noqa: E402

_browser: Browser | None = None
_proc: subprocess.Popen | None = None
_unavailable = ""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _flags(port: int, profile: str) -> list[str]:
    return ["--headless=new", "--no-sandbox", "--disable-gpu", f"--remote-debugging-port={port}",
            f"--user-data-dir={profile}", "--no-first-run", "about:blank"]


def setUpModule() -> None:
    global _browser, _proc, _unavailable
    if os.environ.get("ROMORG_NO_BROWSER"):
        _unavailable = "ROMORG_NO_BROWSER is set"
        return
    port = os.environ.get("ROMORG_BROWSER_PORT")
    if port:
        _browser = Browser.connect(int(port))
        return
    port_n = _free_port()
    profile = tempfile.mkdtemp(prefix="romorg-browser-")
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
        _proc.terminate()


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

    def library_preview(self) -> None:
        self.open()
        self.scan()
        self.page.goto(self.fx.url + "#/system/commodore-amiga/library")
        self.page.wait("!!document.querySelector('#lib-plan-btn')?.offsetParent")
        self.page.eval("document.getElementById('lib-plan-btn').click()")
        self.page.wait("document.querySelectorAll('#lib-table tbody tr').length > 3")

    def names(self, table: str = "lib-table") -> list[str]:
        return self.page.eval(f"""[...document.querySelectorAll('#{table} tbody tr:not(.detail-row)')]
            .map(r => (r.querySelector('.rename-to') || r.cells[1]).textContent.trim().split('/').pop())""")

    def no_js_errors(self) -> None:
        self.assertEqual(self.page.errors, [])


class StartupTests(UiTestCase):
    def test_the_page_asks_for_status_and_platforms_once(self) -> None:
        self.open()
        self.page.wait("document.querySelectorAll('.syscard').length > 3")
        names = self.page.eval("performance.getEntriesByType('resource').map(e => new URL(e.name).pathname)")
        self.assertEqual(names.count("/api/status"), 1)
        self.assertEqual(names.count("/api/platforms"), 1)
        self.no_js_errors()


class LibraryListTests(UiTestCase):
    def test_columns_and_name_sort_both_ways(self) -> None:
        self.library_preview()
        heads = self.page.eval("[...document.querySelectorAll('#lib-table thead th')].map(t => t.textContent.trim())")
        for wanted in ("Change (relative to the system folder)", "Rating", "Year", "Size", "Details"):
            self.assertIn(wanted, heads)
        self.click("#lib-table th button", "Change")
        self.page.wait("document.querySelector('#lib-table th[aria-sort=ascending]')")
        up = self.names()
        self.click("#lib-table th button", "Change")
        self.page.wait("document.querySelector('#lib-table th[aria-sort=descending]')")
        down = self.names()
        self.assertEqual(sorted(up, key=str.casefold), up)
        self.assertEqual(sorted(down, key=str.casefold, reverse=True), down)
        self.click("#lib-table th button", "Change")           # third click clears it
        self.page.wait("!document.querySelector('#lib-table th[aria-sort=descending]')")
        self.no_js_errors()

    def test_year_and_size_sort(self) -> None:
        self.library_preview()
        self.click("#lib-table th button", "Year")
        self.page.wait("document.querySelector('#lib-table th[aria-sort=ascending]')")
        years = self.page.eval("""[...document.querySelectorAll('#lib-table tbody tr:not(.detail-row)')]
            .map(r => r.cells[r.cells.length - 4].textContent.trim()).filter(Boolean).map(Number)""")
        self.assertEqual(years, sorted(years))
        self.click("#lib-table th button", "Size")
        self.page.wait("document.querySelector('#lib-table th[aria-sort=descending]')")
        self.no_js_errors()

    def test_details_panel_explains_the_row_and_shows_checksums(self) -> None:
        self.library_preview()
        self.click("#lib-table .cs-toggle")
        self.page.wait("document.querySelector('#lib-table .why-panel')")
        self.page.wait("document.querySelector('#lib-table .detail-row:not(.hidden) .cs-sums table, "
                       "#lib-table .detail-row:not(.hidden) .cs-sums .cs-note')")
        text = self.page.eval("document.querySelector('#lib-table .detail-row:not(.hidden)').innerText")
        self.assertRegex(text.lower(), r"kept|excluded|better version|set aside")
        self.assertRegex(text.lower(), r"sha1|crc")
        self.no_js_errors()

    def test_always_exclude_a_game_then_recalculate(self) -> None:
        self.library_preview()
        target = "Beta Blaster (1992)(Bits).adf"
        self.page.eval(f"""(() => {{ const r = [...document.querySelectorAll('#lib-table tbody tr')]
            .find(r => r.textContent.includes({target!r})); r.querySelector('.sel-col input').click(); }})()""")
        self.page.wait("!!document.querySelector('#lib-table .sel-bar:not(.hidden)')")
        self.click("#lib-table .sel-bar button", "Always exclude")
        self.page.wait("document.querySelector('#lib-plan-btn').classList.contains('btn-primary')")
        self.click("#lib-plan-btn")
        self.page.wait(f"""[...document.querySelectorAll('#lib-table tbody tr')].some(r =>
            r.textContent.includes({target!r}) && r.textContent.includes('_excluded'))""")
        self.assertIn("always excluded by you", self.page.eval(
            "[...document.querySelectorAll('#lib-table tbody tr')].find(r => r.textContent.includes('Beta Blaster')).innerText").lower())
        self.no_js_errors()


class RememberedViewTests(UiTestCase):
    def test_sort_and_hidden_columns_survive_a_reload(self) -> None:
        self.library_preview()
        self.click("#lib-table th button", "Change")
        self.page.wait("document.querySelector('#lib-table th[aria-sort=ascending]')")
        self.page.eval("document.querySelector('#lib-table .col-menu summary').click()")
        self.page.eval("""[...document.querySelectorAll('#lib-table .col-menu label')]
            .find(l => l.textContent.trim() === 'Size').querySelector('input').click()""")
        self.page.wait("![...document.querySelectorAll('#lib-table thead th')].some(t => t.textContent.trim() === 'Size')")
        self.page.goto(self.fx.url + "#/system/commodore-amiga/library")
        self.page.wait("!!document.querySelector('#lib-plan-btn')?.offsetParent")
        self.page.eval("document.getElementById('lib-plan-btn').click()")
        self.page.wait("document.querySelectorAll('#lib-table tbody tr').length > 3")
        self.assertTrue(self.page.eval("!!document.querySelector('#lib-table th[aria-sort=ascending]')"))
        self.assertFalse(self.page.eval("[...document.querySelectorAll('#lib-table thead th')].some(t => t.textContent.trim() === 'Size')"))
        self.no_js_errors()

    def test_the_columns_menu_closes_on_an_outside_click_and_on_escape(self) -> None:
        self.library_preview()
        is_open = "document.querySelector('#lib-table .col-menu').open"
        self.page.eval("document.querySelector('#lib-table .col-menu summary').click()")
        self.assertTrue(self.page.eval(is_open))
        self.page.eval("document.querySelector('h1, h2').click()")
        self.assertFalse(self.page.eval(is_open))
        self.page.eval("document.querySelector('#lib-table .col-menu summary').click()")
        self.page.eval("document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape'}))")
        self.assertFalse(self.page.eval(is_open))
        self.no_js_errors()

    def test_compact_view_toggle_is_remembered(self) -> None:
        self.open()
        self.click("#density-btn")
        self.page.wait("document.body.classList.contains('dense')")
        self.page.goto(self.fx.url)
        self.page.wait("document.body.classList.contains('dense')")
        self.no_js_errors()


class BrowseListTests(UiTestCase):
    def browse(self, view: str, gba: bool = False) -> None:
        self.open()
        if gba:
            self.scan("Nintendo Game Boy Advance", str(self.fx.gba))
        else:
            self.scan()
        slug = "nintendo-game-boy-advance" if gba else "commodore-amiga"
        self.page.goto(self.fx.url + f"#/system/{slug}/browse?view={view}")
        self.page.wait("!!document.querySelector('#tab-browse')?.offsetParent")
        self.page.wait("document.querySelectorAll('#tab-browse tbody tr').length > 3")

    def column(self, css: str) -> list[str]:
        return self.page.eval(f"[...document.querySelectorAll('#tab-browse tbody tr:not(.detail-row) {css}')].map(e => e.textContent.trim())")

    def test_games_sort_by_name_both_ways(self) -> None:
        self.browse("games", gba=True)
        self.click("#tab-browse th button", "Game")
        self.page.wait("document.querySelector('#tab-browse th[aria-sort=ascending]')")
        up = self.column(".game-name")
        self.assertEqual(up, sorted(up, key=str.casefold))
        self.click("#tab-browse th button", "Game")
        self.page.wait("document.querySelector('#tab-browse th[aria-sort=descending]')")
        down = self.column(".game-name")
        self.assertEqual(down, sorted(down, key=str.casefold, reverse=True))
        self.no_js_errors()

    def test_games_size_sorts_largest_first(self) -> None:
        self.browse("games", gba=True)
        heads = self.page.eval("[...document.querySelectorAll('#tab-browse thead th')].map(t => t.textContent.trim())")
        self.assertIn("Size", heads)
        self.assertNotIn("Year", heads)          # No-Intro names carry no year
        self.click("#tab-browse th button", "Size")
        self.page.wait("document.querySelector('#tab-browse th[aria-sort=descending]')")
        idx = heads.index("Size")
        sizes = self.page.eval(f"[...document.querySelectorAll('#tab-browse tbody tr:not(.detail-row)')].map(r => r.cells[{idx}].textContent.trim())")
        self.assertEqual(sizes[0], "4.0 KB")
        self.assertEqual(sizes[-1], "2.0 KB")
        self.no_js_errors()

    def test_matched_files_sort_by_name(self) -> None:
        self.browse("matched")
        self.click("#tab-browse th button", "Local file")
        self.page.wait("document.querySelector('#tab-browse th[aria-sort=ascending]')")
        names = self.page.eval("[...document.querySelectorAll('#tab-browse tbody tr:not(.detail-row) td:first-child div:first-child')].map(e => e.textContent.trim())")
        self.assertEqual(names, sorted(names, key=str.casefold))
        self.no_js_errors()


if __name__ == "__main__":
    unittest.main()
