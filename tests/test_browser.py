"""Real-browser checks (Chromium via Playwright). Skipped when Playwright or a browser is not installed.
They catch what the fake-DOM test cannot: layout, overflow, console errors, RTL placement and chart interaction."""
import json
import shutil
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent.parent
import sys  # noqa: E402

sys.path.insert(0, str(ROOT))
import app as A  # noqa: E402
import hospital_agents.auth as _auth  # noqa: E402

_auth.ITERATIONS = 2000

try:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as _p:
        _p.chromium.launch().close()
    AVAILABLE = True
except Exception:                                                     # noqa: BLE001 - any failure means "no browser here"
    AVAILABLE = False

PAGES = ["overview", "alerts", "beds", "er", "staff", "or", "forecast", "agents", "simulator", "decisions", "assistant", "sources", "system"]


@unittest.skipUnless(AVAILABLE, "Playwright with Chromium is not installed")
class Browser(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.srv = A.make_server(ROOT / "data", 0, runtime=cls.tmp / "rt", policy_dir=cls.tmp / "pol")
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        A.Handler.app.auth.create_user("mgr", "browser-pass-1", "operations_manager")
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.srv.shutdown()
        cls.srv.server_close()
        shutil.rmtree(cls.tmp)

    def open(self, width, lang):
        ctx = self.browser.new_context(viewport={"width": width, "height": 900})
        ctx.add_init_script(f"localStorage.setItem('lang','{lang}')")
        page, errors = ctx.new_page(), []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: m.type == "error" and "401" not in m.text and errors.append(m.text))
        page.goto(f"http://127.0.0.1:{self.port}/")
        page.fill("#u", "mgr")
        page.fill("#p", "browser-pass-1")
        page.click("#login-form button[type=submit]")
        page.wait_for_selector("#app:not([hidden])")
        self.addCleanup(ctx.close)
        return page, errors

    def visit(self, page, name):
        page.evaluate(f"location.hash='#/{name}'")
        page.wait_for_function("() => !document.querySelector('#view .skeleton') && document.querySelector('#view').children.length", timeout=15000)
        page.wait_for_timeout(250)

    def test_every_page_has_no_console_errors_and_no_horizontal_overflow(self):
        for width, lang in ((1366, "ar"), (1366, "en"), (390, "ar"), (390, "en"), (1920, "ar")):
            page, errors = self.open(width, lang)
            for name in PAGES:
                self.visit(page, name)
                over = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
                self.assertLessEqual(over, 1, f"{name} {lang} {width}: {over}px horizontal overflow")
                self.assertEqual(page.locator("#view .errorbox").count(), 0, f"{name} {lang} {width}")
            self.assertEqual(errors, [], f"{lang} {width}")

    def test_overview_has_real_kpi_cards_a_twin_and_alerts(self):
        page, _ = self.open(1366, "ar")
        self.visit(page, "overview")
        self.assertGreaterEqual(page.locator(".kpi").count(), 8)
        self.assertEqual(page.locator(".zone").count(), 7)
        self.assertEqual(page.locator(".zone .bed").count(), 60)
        self.assertGreaterEqual(page.locator(".alert").count(), 2)
        values = page.locator(".kpi .value").all_inner_texts()
        self.assertTrue(all(any(ch.isdigit() for ch in v) for v in values), values)

    def test_the_sidebar_is_on_the_right_in_arabic_and_on_the_left_in_english(self):
        for lang, right in (("ar", True), ("en", False)):
            page, _ = self.open(1366, lang)
            box = page.locator("#side").bounding_box()
            self.assertEqual(box["x"] > 600, right, (lang, box))
            self.assertEqual(page.evaluate("document.documentElement.dir"), "rtl" if lang == "ar" else "ltr")

    def test_mobile_menu_is_off_canvas_until_opened(self):
        page, _ = self.open(390, "ar")
        self.assertGreaterEqual(page.locator("#side").bounding_box()["x"], 380)
        page.click("#menu")
        page.wait_for_timeout(350)
        self.assertLess(page.locator("#side").bounding_box()["x"], 200)

    def test_chart_hover_shows_a_tooltip_with_a_unit_and_legend_toggles_a_series(self):
        page, _ = self.open(1366, "en")
        self.visit(page, "beds")
        svg = page.locator("#view .chart svg").first
        svg.scroll_into_view_if_needed()                                  # the chart is below the fold at 900px height
        box = svg.bounding_box()
        page.mouse.move(box["x"] + box["width"] * 0.4, box["y"] + box["height"] * 0.5)
        page.wait_for_timeout(200)
        self.assertTrue(page.locator("#tip").is_visible())
        self.assertIn("%", page.locator("#tip").inner_text())
        legend = page.locator("#view .legend button").first
        legend.click()
        self.assertEqual(legend.get_attribute("aria-pressed"), "false")

    def test_buttons_and_filters_work(self):
        page, _ = self.open(1366, "en")
        self.visit(page, "overview")
        page.click(".seg button:nth-child(2)")                       # ER and ICU
        page.wait_for_timeout(150)
        self.assertEqual(page.locator('.zone.dim[data-type="WARD"]').count(), 1)
        self.assertEqual(page.locator('.zone.dim[data-type="ER"]').count(), 0)
        page.click('.zone[data-type="ICU"]')                         # a zone opens its detail page
        page.wait_for_function("location.hash === '#/beds'")
        self.visit(page, "assistant")
        page.fill(".composer textarea", "How is the ER doing?")
        page.click(".composer button")
        page.wait_for_selector(".msg:not(.user) .bubble:not(.typing)", timeout=15000)
        self.assertIn("ER", page.locator(".msgs").inner_text())

    def test_the_api_key_is_never_present_in_the_page(self):
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {"GROQ_API_KEY": "secret-key-xyz", "GROQ_BASE_URL": "http://127.0.0.1:1/v1"}):
            page, _ = self.open(1366, "en")
            self.visit(page, "system")
            page.click("text=Test connection")
            page.wait_for_selector("text=Not connected", timeout=15000)
            self.assertNotIn("secret-key-xyz", page.content())
            self.assertNotIn("secret-key-xyz", page.evaluate("JSON.stringify(sessionStorage) + JSON.stringify(localStorage)"))


if __name__ == "__main__":
    unittest.main()
