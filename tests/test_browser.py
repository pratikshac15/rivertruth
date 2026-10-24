"""Browser tests: the page a person sees is the same for every kind of gauge.

Runs the real page and server against the fakes in headless Chromium (Playwright). Skipped when Playwright or a
browser is not installed. The map library is replaced by a small stand-in because map tiles need the internet.
"""
import os
import time
import unittest

from tests.fakes import Env
from tests.test_integration_server import Server

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    sync_playwright = None

HERE = os.path.dirname(os.path.abspath(__file__))
NAMES = {"TODA1": "Black Warrior River at Tuscaloosa and Northport, Alabama", "CKLA1": "Cahaba River at Centreville",
         "AVLA1": "Tombigbee River at Bevill Lock and Dam", "MMTW2": "Kanawha River below Marmet Locks",
         "PMAF1": "Tide Gauge at Port Manatee (in MHHW)", "DRYX1": "Empty Creek near Nowhere"}
KIND = {"TODA1": "flow", "CKLA1": "flow", "AVLA1": "flow", "MMTW2": "flow", "PMAF1": "level", "DRYX1": "none"}


@unittest.skipIf(sync_playwright is None, "playwright not installed")
class GaugePage(unittest.TestCase):
    def _page(self, p, url):
        b = p.chromium.launch()
        page = b.new_page(viewport={"width": 1280, "height": 1000})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.route("**/fonts.g*/**", lambda r: r.abort())
        page.route("**/leaflet.min.css", lambda r: r.fulfill(body="", content_type="text/css"))
        page.route("**/leaflet.min.js", lambda r: r.fulfill(path=os.path.join(HERE, "assets", "leaflet_mock.js"),
                                                            content_type="application/javascript"))
        page.goto(url)
        page.wait_for_function("window.__markers && window.__markers.length >= 6", timeout=20000)
        return b, page, errors

    def _check(self, page, lid):
        page.evaluate(f"window.__clickGauge('{lid}')")
        page.click("#cGo")
        page.wait_for_function(
            "() => { const h = document.querySelector('#cOut .headline'); return h && h.textContent.trim().length > 0; }",
            timeout=20000)
        page.wait_for_function(f"() => !document.getElementById('gaugeDetail').hidden && "
                               f"document.getElementById('selTitle').textContent === {NAMES[lid]!r}", timeout=20000)

    def test_every_gauge_kind_gets_the_same_page(self):
        with Env(catalog_extra=50) as env, Server(env) as s, sync_playwright() as p:
            b, page, errors = self._page(p, s.url)
            self.assertTrue(page.evaluate("document.getElementById('gaugeDetail').hidden"), "nothing selected at start")
            for lid, kind in KIND.items():
                with self.subTest(lid=lid):
                    self._check(page, lid)
                    self.assertTrue(page.is_visible("#cDeep"), "Investigate button")
                    self.assertTrue(page.is_visible("#chart"), "chart")
                    big = page.text_content("#nowBig").strip()
                    self.assertTrue(big and big != "Not enough data" and big != "Loading…", big)
                    paths = page.eval_on_selector_all("#chart path", "ps => ps.filter(p => (p.getAttribute('d') || '').length > 0).length")
                    axis = page.eval_on_selector_all("#chart text", "ts => ts.map(t => t.textContent)")
                    note = page.text_content("#chartNote")
                    if kind == "none":
                        self.assertEqual(paths, 0)
                        self.assertIn("No data", note)
                    else:
                        self.assertGreaterEqual(paths, 1)
                        self.assertIn("ft" if kind == "level" else "cfs", axis)
                    if kind == "level":
                        self.assertIn("Water level", page.text_content(".legend"))
                        self.assertIn("0.09", big)
            self.assertEqual(errors, [])
            b.close()

    def test_switching_gauges_never_leaves_the_previous_one_on_screen(self):
        with Env(catalog_extra=10) as env, Server(env) as s, sync_playwright() as p:
            b, page, errors = self._page(p, s.url)
            self._check(page, "TODA1")
            self._check(page, "PMAF1")
            self.assertEqual(page.text_content("#selTitle"), NAMES["PMAF1"])
            self.assertNotIn("Northport", page.text_content(".legend"))
            self.assertNotIn("Northport", page.text_content("#selNote"))
            self.assertEqual(errors, [])
            b.close()


if __name__ == "__main__":
    unittest.main()
