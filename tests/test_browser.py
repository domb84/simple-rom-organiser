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

    def test_root_to_clean_library_and_undo(self) -> None:
        base = Path(tempfile.mkdtemp(prefix="romorg-ui-col-"))
        self.addCleanup(shutil.rmtree, base, True)
        roms, dest = base / "roms", base / "library"
        roms.mkdir()
        (roms / "amiga").symlink_to(self.fx.root, target_is_directory=True)
        before = sorted(str(p.relative_to(self.fx.root)) for p in self.fx.root.rglob("*"))
        self.open("#/collection")
        self.page.wait("!document.getElementById('view-collection').classList.contains('hidden')")
        self.assertFalse(self.page.eval("document.getElementById('view-home').offsetParent !== null"))
        self.fill("col-root", str(roms))
        self.page.wait("document.querySelectorAll('#col-systems input[type=checkbox]').length >= 2")
        self.assertTrue(self.page.eval("[...document.querySelectorAll('#col-systems tr')].find(r => r.textContent.includes('Commodore Amiga')).querySelector('input').checked"))
        self.fill("col-dest", str(dest))
        self.click("#col-plan-btn")
        self.page.wait("document.getElementById('col-table').textContent.includes('Commodore Amiga')", timeout=60)
        self.assertFalse(dest.exists())
        self.click("#col-apply-btn")
        self.assertIn("ROM folders keep their files", self.confirm_dialog())
        self.page.wait("document.querySelector('.toast')?.textContent.includes('Built ')", timeout=90)
        self.assertTrue([p for p in dest.rglob("*") if p.is_file() and ".romorg-library" not in p.parts])
        self.assertEqual(sorted(str(p.relative_to(self.fx.root)) for p in self.fx.root.rglob("*")), before)
        self.page.wait("document.getElementById('col-undo-btn').dataset.blocked !== '1'")
        self.click("#col-undo-btn")
        self.assertIn("Remove what the last build added", self.confirm_dialog())
        self.page.wait("document.querySelector('.toast')?.textContent.includes('Removed ')", timeout=30)
        self.assertEqual([p for p in dest.rglob("*") if p.is_file() and ".romorg-library" not in p.parts], [])
        self.no_js_errors()

    def test_sync_removes_what_the_source_lost(self) -> None:
        base = Path(tempfile.mkdtemp(prefix="romorg-ui-sync-"))
        self.addCleanup(shutil.rmtree, base, True)
        roms, dest = base / "roms", base / "library"
        roms.mkdir()
        (roms / "amiga").symlink_to(self.fx.root, target_is_directory=True)
        self.open("#/collection")
        self.page.wait("!document.getElementById('view-collection').classList.contains('hidden')")
        self.fill("col-root", str(roms))
        self.page.wait("document.querySelectorAll('#col-systems input[type=checkbox]').length >= 2")
        self.fill("col-dest", str(dest))
        self.click("#col-apply-btn")
        self.confirm_dialog()
        self.page.wait("document.querySelector('.toast')?.textContent.includes('Built ')", timeout=90)
        built = sorted(p for p in dest.rglob("*") if p.is_file() and ".romorg-library" not in p.parts)
        self.assertGreater(len(built), 2)
        victim = self.fx.root / "Zeta Zone (1995)(Zed).adf"
        victim.unlink()
        self.page.eval("document.getElementById('col-sync').click()")
        self.page.wait("document.getElementById('col-sync').checked")
        self.click("#col-plan-btn")
        self.page.wait("document.getElementById('col-cards').textContent.includes('To remove')", timeout=60)
        self.assertEqual(sorted(p for p in dest.rglob("*") if p.is_file() and ".romorg-library" not in p.parts), built)
        self.click("#col-apply-btn")
        self.assertIn("SYNC is on", self.confirm_dialog())
        self.page.wait("document.querySelector('.toast')?.textContent.includes('Built 0 ')", timeout=90)
        after = sorted(p for p in dest.rglob("*") if p.is_file() and ".romorg-library" not in p.parts)
        self.assertEqual(len(after), len(built) - 1)
        self.no_js_errors()

    def test_shared_rules_are_saved_and_the_home_page_links_here(self) -> None:
        self.open()
        self.assertEqual(self.page.eval("document.getElementById('collection-link').getAttribute('href')"), "#/collection")
        self.page.goto(self.fx.url + "#/collection")
        self.page.wait("document.querySelectorAll('#col-rules input[type=checkbox]').length > 5")
        self.page.eval("[...document.querySelectorAll('#col-rules label')].find(l => l.textContent.includes('One version per game')).querySelector('input').click()")
        self.page.wait("document.getElementById('col-rules-note').textContent.includes('set')")
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
    """The Convert step of a disc system: the writer / compression dropdowns and plan -> apply -> undo with the
    built-in writer (a one-track synthetic PlayStation disc, no chdman, no audio so no libFLAC is needed)."""

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
        self.open()
        self.scan(self.PLATFORM, str(self.psx))
        self.page.goto(self.fx.url + f"#/system/{self.SLUG}/tools")
        self.page.wait("!!document.querySelector('#chdman-box:not(.hidden)') && "
                       "document.getElementById('chdman-line').textContent.length > 5")

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
            self.page.goto(self.fx.url + f"#/system/{self.SLUG}/tools")        # saved on the server: survives a reload
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

    def test_plan_apply_and_undo_with_the_built_in_writer(self) -> None:
        self.tools()
        self.click("#convert-plan-btn")
        self.page.wait("document.querySelectorAll('#convert-table tbody tr').length === 1")
        self.assertIn("Solo (USA).chd", self.page.eval("document.querySelector('#convert-table tbody tr').innerText"))
        self.click("#convert-apply-btn")
        self.assertIn("Convert 1 raw set", self.confirm_dialog())
        self.page.wait("document.querySelector('.toast')?.textContent.includes('Converted 1 file')", timeout=120)
        self.idle()
        chd = self.psx / "Solo (USA)" / "Solo (USA).chd"
        self.assertTrue(chd.is_file())
        self.assertTrue((self.psx / "_converted_originals" / "raw" / "t.bin").is_file())
        self.assertFalse((self.psx / "raw" / "t.bin").exists())
        self.page.wait("document.getElementById('convert-undo-btn').dataset.blocked !== '1'")
        self.click("#convert-undo-btn")
        self.confirm_dialog()
        self.page.wait("(async () => { const j = await fetch('/api/job').then(r => r.json()); "
                       "return j && j.status === 'done' && !!j.result; })()")
        self.idle()
        self.assertFalse(chd.exists())
        self.assertTrue((self.psx / "raw" / "t.bin").is_file())
        self.no_js_errors()


if __name__ == "__main__":
    unittest.main()
