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
from unittest import mock
os.environ.setdefault("ROMORG_RETROARCH_DETECT", "0")      # never pick up a RetroArch installed on this machine

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from cdp import Browser  # noqa: E402
from uifixture import Fixture  # noqa: E402

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
        self.page.wait("document.querySelectorAll('.nav-item[data-platform]').length > 3")
        names = self.page.eval("performance.getEntriesByType('resource').map(e => new URL(e.name).pathname)")
        self.assertEqual(names.count("/api/status"), 1)
        self.assertEqual(names.count("/api/platforms"), 1)
        self.no_js_errors()


class DatabasesPanelTests(UiTestCase):
    def test_every_database_is_listed_with_its_version(self) -> None:
        self.open()
        self.page.wait("document.getElementById('updates-line').textContent.includes('Redump')")
        line = self.page.eval("document.getElementById('updates-line').textContent")
        for source in ("TOSEC", "No-Intro", "WHDLoad", "Redump"):
            self.assertIn(source, line)
        self.click("#databases-btn")
        self.page.wait("document.querySelectorAll('#databases-panel tbody tr').length > 5")
        rows = self.page.eval("[...document.querySelectorAll('#databases-panel tbody tr')].map(r => [...r.cells].map(c => c.textContent.trim()))")
        names = {r[1]: r for r in rows}
        for wanted in ("Sega - Dreamcast", "Sony - PlayStation", "Sony - PlayStation 2", "Nintendo - Game Boy Advance",
                       "Commodore - Amiga - WHDLoad"):
            self.assertIn(wanted, names)
        self.assertEqual(names["Nintendo - Game Boy Advance"][2], "20250101-000000")     # the fixture's DAT version
        self.assertEqual(names["Sega - Dreamcast"][2], "not installed")
        self.assertEqual({r[0] for r in rows}, {"TOSEC", "No-Intro", "WHDLoad", "Redump", "Ratings"})
        self.click("#databases-btn")
        self.assertTrue(self.page.eval("document.getElementById('databases-panel').classList.contains('hidden')"))
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


class BuildElsewhereTests(UiTestCase):
    def confirm_dialog(self) -> str:
        self.page.wait("!document.getElementById('confirm-modal').classList.contains('hidden')")
        text = self.page.eval("document.getElementById('confirm-body').innerText")
        self.page.eval("document.getElementById('confirm-yes').click()")
        return text

    def test_build_in_another_folder_leaves_the_source_alone_and_undoes(self) -> None:
        dest = Path(tempfile.mkdtemp(prefix="romorg-ui-lib-"))
        self.addCleanup(shutil.rmtree, dest, True)
        before = sorted(str(p.relative_to(self.fx.root)) for p in self.fx.root.rglob("*"))
        self.open()
        self.scan()
        self.page.goto(self.fx.url + "#/system/commodore-amiga/library")
        self.page.wait("!!document.querySelector('#lib-where-other')?.offsetParent")
        self.assertTrue(self.page.eval("document.getElementById('lib-export-opts').classList.contains('hidden')"))
        self.page.eval("document.getElementById('lib-where-other').click()")
        self.page.eval(f"""(() => {{ const i = document.getElementById('lib-export-dest'); i.value = {str(dest)!r};
            i.dispatchEvent(new Event('change')); }})()""")
        self.assertEqual(self.page.eval("document.getElementById('lib-apply-btn').textContent"), "Build library in destination")
        self.click("#lib-plan-btn")
        self.page.wait("document.getElementById('lib-cards').textContent.includes('To copy')")
        self.click("#lib-apply-btn")
        self.assertIn("its files stay where they are", self.confirm_dialog())
        self.page.wait("document.querySelector('.toast')?.textContent.includes('Built ')", timeout=60)
        built = [p for p in dest.rglob("*") if p.is_file() and ".romorg-library" not in p.parts]
        self.assertTrue(built)
        self.assertEqual(sorted(str(p.relative_to(self.fx.root)) for p in self.fx.root.rglob("*")), before)
        self.page.goto(self.fx.url + "#/system/commodore-amiga/library")   # the choice is remembered
        self.page.wait("document.getElementById('lib-where-other').checked")
        self.assertEqual(self.page.eval("document.getElementById('lib-export-dest').value"), str(dest))
        self.click("#lib-undo-btn")
        self.assertIn("Remove the", self.confirm_dialog())
        self.page.wait("document.querySelector('.toast')?.textContent.includes('Removed ')", timeout=30)
        self.assertEqual([p for p in dest.rglob("*") if p.is_file() and ".romorg-library" not in p.parts], [])
        self.no_js_errors()


class CollectionTests(UiTestCase):
    def confirm_dialog(self) -> str:
        self.page.wait("!document.getElementById('confirm-modal').classList.contains('hidden')")
        text = self.page.eval("document.getElementById('confirm-body').innerText")
        self.page.eval("document.getElementById('confirm-yes').click()")
        return text

    def fill(self, id_: str, value: str) -> None:
        self.page.eval(f"""(() => {{ const i = document.getElementById({id_!r}); i.value = {value!r};
            i.dispatchEvent(new Event('change')); }})()""")

    def _mixed(self) -> tuple[Path, Path]:
        base = Path(tempfile.mkdtemp(prefix="romorg-ui-col-"))
        self.addCleanup(shutil.rmtree, base, True)
        mixed = base / "mixed"
        (mixed / "Dump").mkdir(parents=True)
        for f in list(self.fx.root.glob("*.adf")):
            shutil.copy(f, mixed / "Dump" / f.name)
        for f in list(self.fx.gba.glob("*.gba"))[:2]:
            shutil.copy(f, mixed / "Dump" / f.name)
        (mixed / "Dump" / "cover.jpg").write_bytes(b"jpg")
        return base, mixed

    def _scan(self, mixed: Path) -> None:
        self.open("#/collection")
        self.page.wait("!document.getElementById('view-collection').classList.contains('hidden')")
        self.assertFalse(self.page.eval("!!document.getElementById('col-detect')"))            # there is no "find the systems" step
        self.assertTrue(self.page.eval("document.getElementById('col-systems-panel').classList.contains('hidden')"))
        self.fill("col-root", str(mixed))
        self.click("#col-scan-btn")
        self.page.wait("document.querySelectorAll('#col-systems tr').length >= 2", timeout=90)
        self.assertEqual(self.page.eval("[...document.querySelectorAll('#col-systems tr')].map(r => r.cells[0].textContent).sort()"),
                         ["Commodore Amiga", "Nintendo Game Boy Advance"])
        self.assertIn("Scanned", self.page.eval("document.getElementById('col-scan-info').textContent"))

    def test_one_scan_then_a_quick_preview_and_sort_and_tidy_and_undo(self) -> None:
        base, mixed = self._mixed()
        before = sorted(str(p.relative_to(mixed)) for p in mixed.rglob("*") if p.is_file())
        self._scan(mixed)
        self.click("#col-plan-btn")
        self.page.wait("document.getElementById('col-cards').textContent.includes('Files to sort into systems')", timeout=60)
        self.assertEqual(sorted(str(p.relative_to(mixed)) for p in mixed.rglob("*") if p.is_file()), before)   # a preview moves nothing
        self.click("#col-apply-btn")
        self.assertIn("standard short names", self.confirm_dialog())
        self.page.wait("[...document.querySelectorAll('.toast')].some(t => t.textContent.includes('Done:'))", timeout=120)
        self.assertTrue(list((mixed / "amiga").rglob("*.adf")) and list((mixed / "gba").rglob("*.gba")))
        self.assertTrue((base / "mixed-archive" / "_other" / "Dump" / "cover.jpg").is_file())
        self.page.wait("document.getElementById('col-undo-btn').dataset.blocked !== '1'")
        self.click("#col-undo-btn")
        self.confirm_dialog()
        self.page.wait("[...document.querySelectorAll('.toast')].some(t => t.textContent.includes('Scan again'))", timeout=60)
        self.assertEqual(sorted(str(p.relative_to(mixed)) for p in mixed.rglob("*") if p.is_file() and not p.name.startswith(".romorg")), before)
        self.no_js_errors()

    def test_a_clean_library_built_elsewhere_from_the_scan(self) -> None:
        base, mixed = self._mixed()
        dest = base / "library"
        before = sorted(str(p.relative_to(mixed)) for p in mixed.rglob("*") if p.is_file())
        self._scan(mixed)
        self.page.eval("document.getElementById('col-place-elsewhere').click()")
        self.page.wait("!document.getElementById('col-elsewhere').classList.contains('hidden')")
        self.fill("col-dest", str(dest))
        self.click("#col-plan-btn")
        self.page.wait("document.getElementById('col-cards').textContent.includes('To copy')", timeout=60)
        self.assertFalse(dest.exists())
        self.click("#col-apply-btn")
        self.assertIn("ROM folder keeps its files", self.confirm_dialog())
        self.page.wait("[...document.querySelectorAll('.toast')].some(t => t.textContent.includes('Built '))", timeout=120)
        self.assertTrue(list((dest / "gba").glob("*.gba")))
        self.assertEqual(sorted(str(p.relative_to(mixed)) for p in mixed.rglob("*") if p.is_file()), before)   # the source is untouched
        self.page.wait("document.getElementById('col-undo-btn').dataset.blocked !== '1'")
        self.click("#col-undo-btn")
        self.confirm_dialog()
        self.page.wait("[...document.querySelectorAll('.toast')].some(t => t.textContent.includes('Removed'))", timeout=60)
        self.assertFalse([p for p in dest.rglob("*") if p.is_file() and ".romorg-library" not in p.parts])
        self.no_js_errors()

    def test_shared_rules_are_saved_and_the_home_page_links_here(self) -> None:
        self.open()
        self.assertEqual(self.page.eval("document.getElementById('collection-link').getAttribute('href')"), "#/collection")
        self.page.goto(self.fx.url + "#/collection")
        self.page.wait("document.querySelectorAll('#col-rules input[type=checkbox]').length > 5")
        self.page.eval("[...document.querySelectorAll('#col-rules label')].find(l => l.textContent.includes('One version per game')).querySelector('input').click()")
        self.page.wait("document.getElementById('col-rules-note').textContent.includes('Shared rules are set')")
        self.page.goto(self.fx.url + "#/collection")
        self.page.eval("location.reload()")
        self.page.wait("document.querySelectorAll('#col-rules input[type=checkbox]').length > 5")
        self.assertFalse(self.page.eval("[...document.querySelectorAll('#col-rules label')].find(l => l.textContent.includes('One version per game')).querySelector('input').checked"))
        self.no_js_errors()


class RetroArchTests(UiTestCase):
    def confirm_dialog(self) -> str:
        self.page.wait("!document.getElementById('confirm-modal').classList.contains('hidden')")
        text = self.page.eval("document.getElementById('confirm-body').innerText")
        self.page.eval("document.getElementById('confirm-yes').click()")
        return text

    def fill(self, id_: str, value: str) -> None:
        self.page.eval(f"""(() => {{ const i = document.getElementById({id_!r}); i.value = {value!r};
            i.dispatchEvent(new Event('change')); }})()""")

    def test_move_saves_and_update_the_config_then_undo(self) -> None:
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
        self.page.wait("document.getElementById('ra-undo-btn').dataset.blocked !== '1'")
        self.click("#ra-undo-btn")
        self.confirm_dialog()
        self.page.wait("[...document.querySelectorAll('.toast')].some(t => t.textContent.includes('back'))", timeout=30)
        self.assertTrue((old / "bsnes" / "Mario (USA).srm").is_file())
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




class SavesUiTests(UiTestCase):
    """RetroArch saves in the UI (Amendment 31): nothing at all without a RetroArch config; with one the Overview, the Library
    tab, the Browse tab and the Collection page show them, and the default / per-game choice is stored."""
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
        self.page.goto(self.fx.url + "#/system/commodore-amiga/library")
        self.page.wait("!!document.querySelector('#lib-plan-btn')?.offsetParent")

    def preview(self) -> None:
        self.page.eval("document.getElementById('lib-plan-btn').click()")
        self.page.wait("document.querySelectorAll('#lib-table tbody tr').length > 3", timeout=30)

    # ---- no RetroArch config: nothing at all
    def test_without_a_retroarch_config_there_is_no_trace_of_saves_anywhere(self) -> None:
        self.open()
        self.scan()
        self.page.goto(self.fx.url + "#/system/commodore-amiga/overview")
        self.page.wait("!!document.querySelector('#summary-cards .card')")
        self.assertTrue(self.page.eval("document.getElementById('ov-saves').classList.contains('hidden')"))
        self.assertEqual(self.page.eval("document.getElementById('ov-saves').textContent"), "")
        self.library_open()
        self.page.wait("!!document.getElementById('library-rules') && document.getElementById('library-rules').children.length > 0")
        self.assertFalse(self.page.eval("!!document.getElementById('lib-saved-games')"))
        self.assertNotIn("aves", self.page.eval("document.getElementById('library-rules').textContent").replace("Saves and", ""))
        self.preview()
        self.assertFalse(self.page.eval("[...document.querySelectorAll('#lib-table thead th')].some(th => th.textContent.includes('Saves'))"))
        self.assertTrue(self.page.eval("document.getElementById('lib-saves-filters').classList.contains('hidden')"))
        self.scan(platform="Nintendo Game Boy Advance", root=str(self.fx.gba))
        self.page.goto(self.fx.url + "#/system/nintendo-game-boy-advance/browse?view=games")
        self.page.wait("document.querySelectorAll('#result-table tbody tr').length > 3")
        self.assertFalse(self.page.eval("[...document.querySelectorAll('#result-table thead th')].some(th => th.textContent.includes('Saves'))"))
        self.assertFalse(self.page.eval("[...document.querySelectorAll('#result-tabs .tab')].some(t => t.textContent.includes('Saves'))"))
        self.assertFalse(self.page.eval("!!document.getElementById('saves-chips')"))
        self.page.goto(self.fx.url + "#/collection")
        self.page.wait("!!document.getElementById('col-rules') && document.getElementById('col-rules').children.length > 0")
        self.assertFalse(self.page.eval("!!document.getElementById('col-saved-games')"))
        self.assertTrue(self.page.eval("document.getElementById('col-saves-th').classList.contains('hidden')"))
        self.no_js_errors()

    # ---- with one
    def test_the_overview_says_how_many_saves_and_which_titles_they_match(self) -> None:
        self.retroarch_with_saves()
        self.open()
        self.scan()
        self.page.goto(self.fx.url + "#/system/commodore-amiga/overview")
        self.page.wait("!document.getElementById('ov-saves').classList.contains('hidden')")
        self.assertEqual(self.page.eval("document.getElementById('ov-saves-line').textContent"),
                         "4 save files for 3 games (1 with a ROM here, 1 without, 1 unmatched)")
        self.assertIn("3 saves \u00b7 1 state", self.page.eval("document.getElementById('ov-saves').textContent"))
        self.shot("overview")
        self.shot("overview", 700)
        self.library_open()
        self.preview()
        self.page.eval("location.hash = '#/system/commodore-amiga/overview'")             # (no reload: the preview stays)
        self.page.wait("!!document.getElementById('ov-saves-plan')")
        self.assertIn("keep 1 game the rules would archive", self.page.eval("document.getElementById('ov-saves-plan').textContent"))
        self.no_js_errors()

    def test_library_rules_wording_the_saves_column_the_filter_and_the_per_game_choice(self) -> None:
        self.retroarch_with_saves()
        self.open()
        self.scan()
        self.library_open()
        self.page.wait("!!document.getElementById('lib-saved-games')")
        self.assertEqual(self.page.eval("document.getElementById('lib-saved-games').value"), "keep")
        self.assertEqual(self.page.eval("[...document.getElementById('lib-saved-games').options].map(o => o.textContent)"),
                         ["Keep both ROMs", "Archive the saves with the ROM", "Leave the saves where they are"])
        self.assertIn("When a rule replaces or archives a game you have saves for", self.page.eval("document.getElementById('lib-saves-group').textContent"))
        self.preview()
        self.page.wait("[...document.querySelectorAll('#lib-table thead th')].some(th => th.textContent.includes('Saves'))")
        self.page.wait("!document.getElementById('lib-saves-filters').classList.contains('hidden')")
        chips = self.page.eval("[...document.querySelectorAll('#lib-saves-filters .chip')].map(c => c.textContent)")
        self.assertEqual(chips, ["Saves: all rows", "Games with saves (1)", "Saves affected by this build (1)"])
        self.shot("library")
        self.shot("library", 700)
        self.click("#lib-saves-filters .chip", "Games with saves")
        self.page.wait("document.querySelectorAll('#lib-table tbody tr:not(.detail-row)').length === 1")
        cell = self.page.eval("document.querySelector('#lib-table .saves-cell').textContent")
        self.assertIn("1 save \u00b7 1 state", cell)
        self.assertIn("ROM kept for them", cell)
        # the choice for this one game: archive its saves with it
        self.assertEqual(self.page.eval("document.querySelector('#lib-table select.saves-choice').value"), "")
        self.select_in_table("archive")
        self.page.wait("[...document.querySelectorAll('.toast')].some(t => t.textContent.includes('press Recalculate'))")
        self.assertEqual(self.api("GET", "/api/library/profile?platform=Commodore%20Amiga")["profile"]["saved_overrides"],
                         [["Commodore Amiga - Games - [ADF]", "Alpha Quest v1.0 (1990)(Acme).adf", "archive"]])
        self.page.eval("document.getElementById('lib-plan-btn').click()")
        self.page.wait("document.querySelector('#lib-table .saves-cell .badge')?.textContent.includes('archived with the ROM')", timeout=30)
        self.assertEqual(self.page.eval("document.querySelector('#lib-table select.saves-choice').value"), "archive")
        self.page.eval("location.reload()")                                              # it is stored: the choice survives
        self.page.wait("!!document.getElementById('lib-saved-games')")
        self.assertEqual(self.api("GET", "/api/library/profile?platform=Commodore%20Amiga")["profile"]["saved_overrides"][0][2], "archive")
        self.select("lib-saved-games", "leave")
        self.page.wait("document.getElementById('lib-saved-games-note').textContent.includes('its saves are not touched')")
        self.assertEqual(self.api("GET", "/api/library/profile?platform=Commodore%20Amiga")["profile"]["saved_games"], "leave")
        self.assertIn("saves left alone", self.page.eval("document.getElementById('library-rules-summary').textContent"))
        self.no_js_errors()

    def select_in_table(self, value: str) -> None:
        self.page.eval(f"""(() => {{ const s = document.querySelector('#lib-table select.saves-choice'); s.value = {value!r};
            s.dispatchEvent(new Event('change')); }})()""")

    def test_browse_has_the_saves_column_the_title_total_the_filter_the_sort_and_the_saves_list(self) -> None:
        self.retroarch_with_saves()
        self.open()
        self.scan(platform="Nintendo Game Boy Advance", root=str(self.fx.gba))
        self.page.goto(self.fx.url + "#/system/nintendo-game-boy-advance/browse?view=games")
        self.page.wait("[...document.querySelectorAll('#result-table thead th')].some(th => th.textContent.includes('Saves'))")
        rows = self.page.eval("""[...document.querySelectorAll('#result-table tbody tr:not(.detail-row)')].map(r => ({
            name: r.querySelector('.game-name')?.textContent, saves: r.querySelector('.saves-cell')?.textContent || ''}))""")
        by = {r["name"]: r["saves"] for r in rows}
        self.assertTrue(by["Alpha Run (USA)"].startswith("3"), by)                            # 1 save + 2 states (the screenshot is not counted)
        self.assertIn("1 save \u00b7 2 states", by["Alpha Run (USA)"])
        self.assertIn("all editions: 4", by["Alpha Run (USA)"])                               # + the Europe edition's save
        self.shot("browse-games")
        self.shot("browse-games", 700)
        self.assertTrue(by["Zeta Zap (Europe)"].startswith("1"), by)                          # a title you have no ROM of
        self.click("#saves-chips .chip", "Games with saves")
        self.page.wait("document.querySelectorAll('#result-table tbody tr:not(.detail-row)').length === 3")
        self.click("#result-table thead th", "Saves")
        self.page.wait("document.querySelector('#result-table tbody tr .game-name')?.textContent === 'Alpha Run (USA)'")
        self.click("#result-tabs .tab", "Saves")
        self.page.wait("document.querySelectorAll('#result-table tbody tr:not(.detail-row)').length === 3")
        text = self.page.eval("document.getElementById('result-table').textContent")
        self.assertIn("ROM here", text)
        self.assertIn("title, no ROM here", text)
        self.shot("browse-saves")
        self.no_js_errors()

    def test_the_collection_page_has_a_saves_column_and_the_rules_have_the_option(self) -> None:
        self.retroarch_with_saves()
        self.api("POST", "/api/collection/save", {"root": str(self.fx.tmp)})
        self.api("POST", "/api/collection/scan", {})
        for _ in range(300):
            if self.api("GET", "/api/job")["status"] != "running":
                break
            time.sleep(0.1)
        self.open("#/collection")
        self.page.wait("!!document.getElementById('col-saved-games')")
        self.page.wait("!document.getElementById('col-saves-th').classList.contains('hidden')")
        cells = self.page.eval("[...document.querySelectorAll('#col-systems td[data-saves-of]')].map(td => [td.dataset.savesOf, td.textContent])")
        self.assertEqual(dict(cells)["Nintendo Game Boy Advance"], "5 (3 games)")
        self.assertIn("Saves (RetroArch):", self.page.eval("document.getElementById('col-saves-line').textContent"))
        self.assertEqual(self.page.eval("document.getElementById('col-saved-games').value"), "keep")
        self.select("col-saved-games", "leave")
        self.page.wait("document.getElementById('col-rules-note').textContent.includes('Shared rules are set')")
        self.assertEqual(self.api("GET", "/api/collection")["global"]["saved_games"], "leave")
        self.assertIn("Building into another folder archives nothing", self.page.eval("document.getElementById('col-rules').textContent"))
        self.shot("collection")
        self.shot("collection", 700)
        self.no_js_errors()

    def test_the_retroarch_switch_is_the_same_setting_as_the_one_in_the_rules(self) -> None:
        self.retroarch_with_saves()
        self.open()
        self.scan()
        self.library_open()
        self.page.wait("!!document.getElementById('lib-saves-follow')")
        self.assertTrue(self.page.eval("document.getElementById('lib-saves-follow').checked"))
        self.page.eval("document.getElementById('lib-saves-follow').click()")
        self.page.wait("!document.getElementById('lib-saves-follow').checked")
        for _ in range(50):                                                              # (the click saves in the background)
            if not self.api("GET", "/api/retroarch")["follow"]:
                break
            time.sleep(0.1)
        self.assertFalse(self.api("GET", "/api/retroarch")["follow"])
        self.page.goto(self.fx.url + "#/retroarch")
        self.page.wait("!!document.getElementById('ra-follow')")
        self.page.wait("document.getElementById('ra-follow').checked === false", timeout=10)
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
        self.library_preview()
        self.page.eval("document.getElementById('lib-aside-on').click()")
        self.page.eval(f"""(() => {{ const i = document.getElementById('lib-aside-dir'); i.value = {str(aside)!r};
            i.dispatchEvent(new Event('change')); }})()""")
        self.click("#lib-plan-btn")
        self.page.wait("document.getElementById('lib-cards').textContent.includes('To move out to the archive folder')", timeout=30)
        self.click("#lib-apply-btn")
        self.assertIn("is then moved out of this folder", self.confirm_dialog())
        self.page.wait("[...document.querySelectorAll('.toast')].some(t => t.textContent.includes('moved out to'))", timeout=60)
        self.assertFalse([p for p in self.fx.root.glob("_*")])
        self.assertTrue(any(aside.rglob("*.adf")))
        self.assertRegex(self.page.eval("document.querySelector('#job-bar .job-time')?.textContent || ''"), r"took \d+s|running")
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

    def test_the_library_converts_raw_discs_first_and_undo_reverts_it(self) -> None:
        self.open()
        self.scan(self.PLATFORM, str(self.psx))
        self.page.goto(self.fx.url + f"#/system/{self.SLUG}/library")
        self.page.wait("!!document.querySelector('#lib-convert-row:not(.hidden)')")
        self.assertFalse(self.page.eval("document.getElementById('lib-convert').checked"))
        self.page.eval("document.getElementById('lib-convert').click()")
        self.page.wait("[...document.querySelectorAll('#lib-convert-row')].length === 1")
        self.click("#lib-plan-btn")
        self.page.wait("document.getElementById('lib-cards').textContent.includes('Raw discs to convert to CHD first')", timeout=60)
        self.click("#lib-apply-btn")
        self.assertIn("converted to CHD first", self.confirm_dialog())
        self.page.wait("[...document.querySelectorAll('.toast')].some(t => t.textContent.includes('converted first'))", timeout=180)
        self.idle()
        chd = self.psx / "Solo (USA)" / "Solo (USA).chd"
        self.assertTrue(chd.is_file())
        self.assertTrue((self.psx / "_converted_originals" / "raw" / "t.bin").is_file())
        self.assertFalse((self.psx / "raw" / "t.bin").exists())
        self.page.wait("document.getElementById('lib-undo-btn').dataset.blocked !== '1'")
        self.click("#lib-undo-btn")
        self.confirm_dialog()
        self.page.wait("[...document.querySelectorAll('.toast')].some(t => t.textContent.includes('Reverted'))", timeout=120)
        self.idle()
        self.assertFalse(chd.exists())
        self.assertTrue((self.psx / "raw" / "t.bin").is_file())
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
      if (r.patch) { const res = await real(url, opts); return json(res.status, Object.assign(await res.json(), r.patch)); }
      if (r.pick) return json(200, { path: r.pick + ((sent || {}).title || '') });
      return json(r.status || 200, r.body);
    }
    return real(url, opts);
  };
})()"""

# every "Browse..." button of the UI (the native folder dialog) and the field its answer belongs in
NATIVE_BUTTONS = {
    "folder-native-btn": "folder-input", "lib-export-native-btn": "lib-export-dest", "lib-aside-native": "lib-aside-dir",
    "col-root-native": "col-root", "col-dest-native": "col-dest", "col-aside-native": "col-aside",
    "ra-custom-native": "ra-custom", "ra-save-native": "ra-save-dir", "ra-state-native": "ra-state-dir",
    "ra-backup-native": "ra-backup-dir", "ra-shared-native": "ra-shared-base", "ra-bios-native": "ra-bios-search",
}
LINUX_ONLY = r"~/|/home/deck|/run/media|Steam Deck|Discover|[Ff]latpak|fusermount|/path/to"


class PlatformTests(UiTestCase):
    """What differs between a Windows server and a Linux one, and what must not: wording, example paths, the folder
    dialog buttons, the layout of a narrow window. The page learns the platform from ``/api/status`` (``os``)."""

    def stub(self, *rules: dict) -> None:
        import json
        self.page.call("Page.addScriptToEvaluateOnNewDocument", source=_FETCH_STUB % json.dumps(list(rules)))

    def show(self, hash_: str) -> None:
        self.page.eval(f"location.hash = {hash_!r}")
        self.page.wait(f"location.hash === {hash_!r}")
        time.sleep(0.5)

    def test_a_windows_server_shows_windows_paths_and_no_linux_wording(self) -> None:
        self.stub({"path": "/api/status", "patch": {"os": "windows", "dialog_available": True}},
                  {"path": "/api/chdman", "patch": {"os": "windows"}})
        self.open("#/collection")
        self.page.wait("document.getElementById('col-root').placeholder.includes('Emulation')")
        self.page.wait("document.querySelectorAll('.nav-item[data-platform]').length > 3")
        self.assertEqual(self.page.eval("document.getElementById('col-root').placeholder"), "D:\\Emulation\\roms")
        self.assertEqual(self.page.eval("document.getElementById('col-dest').placeholder"), "D:\\Emulation\\library")
        found = []
        for view in ("#/collection", "#/retroarch", "#/chd", "#/system/commodore-amiga/overview",
                     "#/system/commodore-amiga/library", "#/system/sony-playstation/overview"):
            self.show(view)
            found += self.page.eval(r"""(() => { const bad = new RegExp(%r), out = [];
              for (const e of document.querySelectorAll('input[placeholder]')) if (bad.test(e.placeholder)) out.push(e.id + ': ' + e.placeholder);
              for (const e of document.querySelectorAll('[title]')) if (bad.test(e.title)) out.push('title: ' + e.title);
              const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
              while (w.nextNode()) if (bad.test(w.currentNode.textContent)) out.push(w.currentNode.textContent.trim().slice(0, 120));
              return out; })()""" % LINUX_ONLY)
        self.assertEqual(sorted(set(found)), [])
        self.assertIn("D:\\Emulation\\roms\\psx", self.page.eval("document.getElementById('folder-input').placeholder"))
        self.no_js_errors()

    def test_a_linux_server_keeps_its_own_example_paths(self) -> None:
        self.stub({"path": "/api/status", "patch": {"os": "linux"}}, {"path": "/api/chdman", "patch": {"os": "linux"}})
        self.open("#/collection")
        self.page.wait("document.querySelectorAll('.nav-item[data-platform]').length > 3")
        self.assertEqual(self.page.eval("document.getElementById('col-root').placeholder"), "~/Emulation/roms")
        self.assertEqual(self.page.eval("document.getElementById('col-dest').placeholder"), "~/Emulation/library")
        self.show("#/system/commodore-amiga/overview")
        self.assertEqual(self.page.eval("document.getElementById('folder-input').placeholder"), "/run/media/deck/<SD>/roms/amiga")
        self.show("#/chd")
        self.page.wait("document.getElementById('chdman-path').placeholder.includes('chdman')")
        self.assertIn("/path/to/chdman", self.page.eval("document.getElementById('chdman-path').placeholder"))
        self.no_js_errors()

    def test_every_browse_button_opens_the_dialog_for_its_own_field(self) -> None:
        ra = self.fx.tmp / "ra"
        ra.mkdir()
        (ra / "retroarch.cfg").write_text('savefile_directory = "default"\n')
        self.stub({"path": "/api/status", "patch": {"dialog_available": True}},
                  {"path": "/api/fs/pick", "method": "POST", "pick": "X:\\picked\\"},
                  {"path": "/api/folders", "method": "POST", "body": {"folders": {}}},
                  {"path": "/api/collection/save", "method": "POST", "status": 400, "body": {"error": "not saved in this test"}})
        self.open("#/retroarch")
        self.page.wait("!document.getElementById('view-retroarch').classList.contains('hidden')")
        self.page.wait("document.querySelectorAll('.nav-item[data-platform]').length > 3")
        self.page.eval(f"(() => {{ const i = document.getElementById('ra-custom'); i.value = {str(ra)!r}; }})()")
        self.click("#ra-custom-add")
        self.page.wait("!document.getElementById('ra-change-panel').classList.contains('hidden')")
        self.show("#/collection")
        self.show("#/system/commodore-amiga/overview")
        ids = self.page.eval("[...document.querySelectorAll('button')].filter(b => b.textContent.trim() === 'Browse...').map(b => b.id).sort()")
        self.assertEqual(ids, sorted(NATIVE_BUTTONS))                      # no Browse button this test does not know
        hidden = self.page.eval("[...document.querySelectorAll('button')].filter(b => b.textContent.trim() === 'Browse...' "
                                "&& b.classList.contains('hidden')).map(b => b.id)")
        self.assertEqual(hidden, [])                                       # the server has a dialog: all of them show
        # every field with a "Folders..." button (the in-app browser) has the native one next to it
        self.assertEqual(self.page.eval("[...document.querySelectorAll('button')].filter(b => b.textContent.trim() === 'Folders...').length"),
                         len(NATIVE_BUTTONS))
        for button, field in NATIVE_BUTTONS.items():
            with self.subTest(button=button):
                self.page.eval(f"""(() => {{ window.__calls.length = 0; document.getElementById({field!r}).value = 'start of {field}';
                    document.getElementById({button!r}).click(); }})()""")
                self.page.wait(f"document.getElementById({field!r}).value.startsWith('X:')")
                asked = self.page.eval("window.__calls.filter(c => c.path === '/api/fs/pick')")
                self.assertEqual(len(asked), 1)
                self.assertEqual(asked[0]["body"]["start"], f"start of {field}")       # the dialog opens where the field points
                self.assertTrue(asked[0]["body"]["title"].startswith("Choose"), asked[0])
                got = self.page.eval(f"document.getElementById({field!r}).value")
                self.assertEqual(got, "X:\\picked\\" + asked[0]["body"]["title"])
                others = self.page.eval(f"""{list(NATIVE_BUTTONS.values())!r}.filter(id => id !== {field!r}
                    && document.getElementById(id).value === {got!r})""")
                self.assertEqual(others, [])                                           # and only that field got the answer
        self.no_js_errors()

    def test_browse_buttons_are_hidden_without_a_dialog_and_a_refusal_is_a_message(self) -> None:
        self.stub({"path": "/api/status", "patch": {"dialog_available": False}},
                  {"path": "/api/fs/pick", "method": "POST", "status": 409, "body": {"error": "A folder dialog is already open"}})
        self.open("#/collection")
        self.page.wait("document.querySelectorAll('.nav-item[data-platform]').length > 3")
        self.show("#/retroarch")
        shown = self.page.eval("[...document.querySelectorAll('button')].filter(b => b.textContent.trim() === 'Browse...' "
                               "&& !b.classList.contains('hidden')).map(b => b.id)")
        self.assertEqual(shown, [])
        # a dialog that is already open (HTTP 409): a message, and the field keeps its text
        self.page.eval("document.getElementById('col-root').value = 'kept'; document.getElementById('col-root-native').click();")
        self.page.wait("[...document.querySelectorAll('#toasts .toast')].some(t => t.textContent.includes('already open'))")
        self.assertEqual(self.page.eval("document.getElementById('col-root').value"), "kept")
        self.assertFalse(self.page.eval("document.getElementById('col-root-native').disabled"))
        self.no_js_errors()

    def test_a_narrow_window_fits_every_page_and_the_system_list_opens_and_closes(self) -> None:
        self.open()
        self.page.wait("document.querySelectorAll('.nav-item[data-platform]').length > 3")
        self.scan()
        for width in (700, 420):
            self.page.call("Emulation.setDeviceMetricsOverride", width=width, height=900, deviceScaleFactor=1, mobile=False)
            for view in ("#/", "#/collection", "#/retroarch", "#/chd", "#/system/commodore-amiga/overview",
                         "#/system/commodore-amiga/library", "#/system/commodore-amiga/browse"):
                with self.subTest(width=width, view=view):
                    self.show(view)
                    over = self.page.eval("document.documentElement.scrollWidth - document.documentElement.clientWidth")
                    self.assertLessEqual(over, 0, "the page is wider than the window")
        self.show("#/collection")                                       # a page is open: the list is folded away
        self.assertEqual(self.page.eval("document.getElementById('shell').dataset.side"), "closed")
        self.click("#side-toggle")
        self.page.wait("document.getElementById('shell').dataset.side === 'open'")
        self.assertEqual(self.page.eval("document.querySelectorAll('.nav-item[data-platform]').length"), 18)
        last = "[...document.querySelectorAll('.nav-item[data-platform]')].pop()"
        self.assertTrue(self.page.eval(f"(() => {{ const r = {last}.getBoundingClientRect(); return r.width > 100 && r.right <= innerWidth; }})()"))
        self.page.eval(f"{last}.click()")                                # picking a system closes the list again
        self.page.wait("document.getElementById('shell').dataset.side === 'closed' && location.hash.startsWith('#/system/')")
        self.page.call("Emulation.clearDeviceMetricsOverride")
        self.no_js_errors()


class RobustnessTests(UiTestCase):
    """Double clicks, failing answers and hostile names: a message, never an exception, never markup."""

    stub = PlatformTests.stub

    def test_a_double_click_on_scan_starts_one_job_and_a_single_step_shows_its_percentage(self) -> None:
        self.stub()
        self.open("#/collection")
        self.page.wait("!document.getElementById('view-collection').classList.contains('hidden')")
        self.page.wait("document.querySelectorAll('.nav-item[data-platform]').length > 3")
        self.page.eval(f"""(() => {{ document.getElementById('col-root').value = {str(self.fx.gba)!r};
            const b = document.getElementById('col-scan-btn'); b.click(); b.click(); b.click(); }})()""")
        self.page.wait("document.querySelectorAll('#col-systems tr').length >= 1", timeout=90)
        self.assertEqual(self.page.eval("window.__calls.filter(c => c.path === '/api/collection/scan').length"), 1)
        self.assertEqual(self.page.eval("[...document.querySelectorAll('#toasts .toast.error')].map(t => t.textContent)"), [])
        self.page.wait("document.querySelector('#job-bar .job-msg')?.textContent === 'Finished'")
        self.assertNotIn("/ 1", self.page.eval("document.querySelector('#job-bar .job-count').textContent"))   # not "1 / 1"
        self.assertEqual(self.page.eval("document.querySelector('#job-bar .progress-bar').style.width"), "100%")
        self.assertRegex(self.page.eval("document.querySelector('#job-bar .job-time').textContent"), r"^took \d+s · [\d.]+ KB scanned$")
        self.no_js_errors()

    def test_a_failing_server_gives_messages_not_exceptions(self) -> None:
        self.stub({"path": "/api/collection", "status": 500, "body": {"error": "RuntimeError: boom"}},
                  {"path": "/api/retroarch", "status": 500, "body": {"error": "RuntimeError: bang"}},
                  {"path": "/api/chdman", "status": 500, "body": {"error": "RuntimeError: crash"}})
        self.open("#/collection")
        self.page.wait("[...document.querySelectorAll('#toasts .toast')].some(t => t.textContent.includes('boom'))")
        # the page has no settings, but Scan still works; when the job ends the page must cope with having none
        self.page.eval(f"""(() => {{ document.getElementById('col-root').value = {str(self.fx.gba)!r};
            document.getElementById('col-scan-btn').click(); }})()""")
        self.page.wait("document.querySelector('#job-bar .job-msg')?.textContent === 'Finished'", timeout=90)
        time.sleep(0.5)
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
        odd.mkdir()
        (odd / "dump.gba").write_bytes(data)
        self.open()
        self.scan("Nintendo Game Boy Advance", str(self.fx.gba))
        shown = "document.querySelector('main').innerText.includes('<img src=x onerror')"
        for view in ("overview", "browse?view=games", "browse?view=unmatched", "browse?view=matched"):
            self.page.goto(self.fx.url + "#/system/nintendo-game-boy-advance/" + view)
            self.page.wait("!document.getElementById('view-system').classList.contains('hidden')")
            if view.startswith("browse"):
                self.page.wait("document.querySelector('#result-table table, #result-table .empty')")
        self.page.wait(shown)
        self.click("#result-table .cs-toggle")
        self.page.wait("document.querySelector('#result-table .detail-row:not(.hidden) .cs-sums table')")
        self.page.goto(self.fx.url + "#/system/nintendo-game-boy-advance/library")
        self.page.wait("!!document.querySelector('#lib-plan-btn')?.offsetParent")
        self.click("#lib-plan-btn")
        # the new file name is made safe for the file system; the row and its details still carry the DAT's name
        self.page.wait("document.querySelector('#lib-table').innerText.includes('dump.gba')")
        self.click("#lib-table .cs-toggle")
        self.page.wait("document.querySelector('#lib-table .detail-row:not(.hidden) .cs-sums table')")
        self.page.goto(self.fx.url + "#/collection")
        self.page.wait("!document.getElementById('view-collection').classList.contains('hidden')")
        self.page.wait("document.querySelectorAll('.nav-item[data-platform]').length > 3")
        self.page.eval(f"""(() => {{ document.getElementById('col-root').value = {str(self.fx.gba)!r};
            document.getElementById('col-scan-btn').click(); }})()""")
        self.page.wait("document.querySelectorAll('#col-systems tr').length >= 1", timeout=90)
        self.click("#col-plan-btn")
        self.page.wait("!document.getElementById('col-output').classList.contains('hidden')", timeout=60)
        self.assertIsNone(self.page.eval("window.__xss === undefined ? null : window.__xss"))
        self.assertEqual(self.page.eval("document.querySelectorAll('img, #xss-b, main script').length"), 0)
        self.no_js_errors()


if __name__ == "__main__":
    unittest.main()
