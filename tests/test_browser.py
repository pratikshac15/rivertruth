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


@unittest.skipIf(sync_playwright is None, "playwright not installed")
class Design(unittest.TestCase):
    """The product design: one quick answer first, the agent team one click away, one visual system, phone-sized too."""

    _page = GaugePage._page
    _check = GaugePage._check

    def test_first_screen_shows_the_promise_and_sponsors_and_no_empty_panels(self):
        with Env(catalog_extra=10) as env, Server(env) as s, sync_playwright() as p:
            b, page, errors = self._page(p, s.url)
            head = page.text_content("header")
            for name in ("Zetaris", "Meterless", "Nemotron"):
                self.assertIn(name, head)
            self.assertFalse(page.is_visible("#deep"), "agent-team section waits for a click")
            self.assertFalse(page.is_visible("#invArea"), "no empty investigation panels")
            self.assertFalse(page.is_visible("#report"))
            self.assertEqual(page.text_content("#reachName").strip(), "", "no river named before one is picked")
            box, tools = page.locator("#map").bounding_box(), page.locator("#locate").bounding_box()
            self.assertTrue(box["x"] <= tools["x"] and tools["y"] + tools["height"] <= box["y"] + box["height"]
                            and tools["y"] >= box["y"], "search sits on the map")
            self.assertEqual(errors, [])
            b.close()

    def test_the_agent_team_opens_on_request_and_shows_its_work_when_running(self):
        with Env(catalog_extra=10) as env, Server(env) as s, sync_playwright() as p:
            b, page, errors = self._page(p, s.url)
            self._check(page, "TODA1")
            self.assertIn("3–5 min", page.text_content("#cDeep"))
            self.assertFalse(page.is_visible("#deep"))
            page.click("#cDeep")
            page.wait_for_selector("#deep", state="visible", timeout=5000)
            self.assertFalse(page.is_visible("#invArea"), "panels appear once a run starts")
            page.fill("#q", "Why did the sources disagree?")
            page.click("#go")
            page.wait_for_selector("#invArea", state="visible", timeout=5000)
            page.wait_for_function("() => document.getElementById('report').textContent.includes('release')", timeout=30000)
            self.assertEqual(errors, [])
            b.close()

    def test_one_typeface_for_the_answer(self):
        with Env(catalog_extra=10) as env, Server(env) as s, sync_playwright() as p:
            b, page, errors = self._page(p, s.url)
            self._check(page, "CKLA1")
            body = page.evaluate("getComputedStyle(document.body).fontFamily")
            head = page.evaluate("getComputedStyle(document.querySelector('#cOut .headline')).fontFamily")
            self.assertEqual(head, body)
            b.close()

    def test_wide_screens_use_the_width_but_paragraphs_stay_readable(self):
        with Env(catalog_extra=10) as env, Server(env) as s, sync_playwright() as p:
            b = p.chromium.launch()
            page = b.new_page(viewport={"width": 1920, "height": 1080})
            page.route("**/fonts.g*/**", lambda r: r.abort())
            page.route("**/leaflet.min.css", lambda r: r.fulfill(body="", content_type="text/css"))
            page.route("**/leaflet.min.js", lambda r: r.fulfill(path=os.path.join(HERE, "assets", "leaflet_mock.js"),
                                                                content_type="application/javascript"))
            page.goto(s.url)
            page.wait_for_function("window.__markers && window.__markers.length >= 6", timeout=20000)
            self.assertGreaterEqual(page.locator("#map").bounding_box()["width"], 1380)
            self.assertLessEqual(page.locator(".hero p").bounding_box()["width"], 760)
            b.close()

    def test_phone_width_has_no_sideways_scrolling(self):
        with Env(catalog_extra=10) as env, Server(env) as s, sync_playwright() as p:
            b = p.chromium.launch()
            page = b.new_page(viewport={"width": 390, "height": 844})
            page.route("**/fonts.g*/**", lambda r: r.abort())
            page.route("**/leaflet.min.css", lambda r: r.fulfill(body="", content_type="text/css"))
            page.route("**/leaflet.min.js", lambda r: r.fulfill(path=os.path.join(HERE, "assets", "leaflet_mock.js"),
                                                                content_type="application/javascript"))
            page.goto(s.url)
            page.wait_for_function("window.__markers && window.__markers.length >= 6", timeout=20000)
            self._check(page, "TODA1")
            self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 390)
            b.close()


if __name__ == "__main__":
    unittest.main()
