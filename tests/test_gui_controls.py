"""Execute the real embedded JavaScript, with mocked APIs (never os._exit)."""
import ast
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest

from test_answer_safety import SafetyTest

ROOT = Path(__file__).resolve().parents[1]
PAGE = next(ast.literal_eval(node.value) for node in ast.parse((ROOT / "gui.py").read_text(encoding="utf-8")).body
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "PAGE" for t in node.targets))


@unittest.skipUnless(shutil.which("node"), "Node.js required for offline JavaScript unit tests")
class GUIControlTests(unittest.TestCase):
    def run_ui(self, **options):
        result = subprocess.run([shutil.which("node"), str(ROOT / "tests" / "gui_runtime.cjs")],
            input=json.dumps({"page": PAGE, **options}), text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_quit_posts_exit_without_changing_or_persisting_language(self):
        for language, message in [("en", "The app is closed"), ("tr", "Uygulama kapandı")]:
            result = self.run_ui(scenario="quit", saved=language)
            self.assertEqual(result["lang"], language)
            self.assertEqual(result["stored"], language)
            self.assertIn(message, result["body"])
            calls = [r for r in result["requests"] if r["path"] == "/api/quit"]
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0]["method"], "POST")
            self.assertEqual(calls[0]["body"], "{}")
            self.assertEqual(calls[0]["headers"]["X-Token"], "__TOKEN__")
            self.assertFalse(result["quitActive"])

    def test_language_buttons_switch_and_preserve_english_selection(self):
        result = self.run_ui(scenario="switch", saved="en")
        self.assertEqual([s["lang"] for s in result["switched"]], ["tr", "en"])
        self.assertEqual([s["stored"] for s in result["switched"]], ["tr", "en"])
        self.assertEqual([s["quitText"] for s in result["switched"]], ["Kapat", "Quit"])
        self.assertTrue(all(s["active"] and s["english"] == "3" for s in result["switched"]))

    def test_invalid_stored_language_uses_english_default(self):
        for value in ["undefined", "ru", "constructor", "__proto__", "", None]:
            with self.subTest(saved=value):
                self.assertEqual(self.run_ui(saved=value)["lang"], "en")
        self.assertEqual(self.run_ui(saved="invalid", navigator="tr-TR")["lang"], "en")

    def test_valid_saved_language_overrides_browser_default(self):
        self.assertEqual(self.run_ui(saved="en", navigator="tr-TR")["lang"], "en")
        self.assertEqual(self.run_ui(saved="tr", navigator="en-US")["lang"], "tr")

    def test_english_is_default_even_with_turkish_browser(self):
        result = self.run_ui(navigator="tr-TR")
        self.assertEqual(result['lang'], 'en')
        self.assertEqual(result['quitText'], 'Quit')
        self.assertIsNone(result['stored'])

    def test_english_default_survives_blocked_storage_and_turkish_browser(self):
        result = self.run_ui(navigator='tr-TR', blockStorage=True)
        self.assertEqual(result['lang'], 'en')
        self.assertEqual(result['quitText'], 'Quit')

    def test_disabled_storage_does_not_break_switching(self):
        result = self.run_ui(scenario="blocked-switch", blockStorage=True, navigator="en-US")
        self.assertEqual([s["lang"] for s in result["switched"]], ["tr", "en"])

    def test_invalid_language_button_value_is_ignored(self):
        result = self.run_ui(scenario="invalid-button", saved="tr")
        self.assertEqual(result["lang"], "tr")
        self.assertEqual(result["stored"], "tr")

    def test_invalid_cached_profile_has_a_clear_message_in_both_languages(self):
        for lang, message in [("en", "Invalid profile"), ("tr", "Geçersiz profil")]:
            result = self.run_ui(saved=lang, profileError="profile.skills: expected array")
            self.assertIn(message, result["message"])
            self.assertIn("profile.skills", result["message"])

    def test_quit_cancels_polling_and_ignores_inflight_response(self):
        for scenario in ["quit-polling", "quit-polling-error"]:
            result = self.run_ui(scenario=scenario, running=True, saved="en")
            self.assertEqual(result["timers"], 0)
            self.assertIn("The app is closed", result["body"])
            self.assertNotIn("late log", result["body"])


@unittest.skipUnless(os.getenv("AUTOAPPLY_BROWSER_TESTS") == "1", "optional local Chrome UI tests")
class BrowserGUIControlTests(SafetyTest):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.pw = sync_playwright().start()
        cls.addClassCleanup(cls.pw.stop)
        cls.browser = cls.pw.chromium.launch(channel="chrome", headless=True)
        cls.addClassCleanup(cls.browser.close)

    def setUp(self):
        super().setUp()
        self.context = self.browser.new_context(locale="en-US")
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.errors, self.quit_requests = [], []
        self.profile_error = ""
        self.start_error = None
        self.page.on("pageerror", lambda error: self.errors.append(error))
        def respond(route):
            path = route.request.url.split("autoapply.test", 1)[-1]
            if path == "/":
                route.fulfill(content_type="text/html", body=PAGE)
            elif path == "/api/state":
                route.fulfill(json={"settings": {}, "cv_name": "", "has_key": False,
                                    "default_locations": ["Worldwide"], "profile": None, "running": False,
                                    "profile_error": self.profile_error})
            elif path == "/api/start" and self.start_error:
                route.fulfill(json=self.start_error)
            elif path == "/api/quit":
                self.quit_requests.append(route.request)
                route.fulfill(json={"ok": True})
            else:
                route.abort()
        self.context.route("**/*", respond)

    def test_switch_then_quit_uses_real_handlers(self):
        self.page.goto("http://autoapply.test/")
        self.page.wait_for_function("document.getElementById('english').value === '3'")
        self.page.locator('[data-l="tr"]').click()
        self.assertEqual(self.page.locator("#quit").inner_text(), "Kapat")
        self.page.locator('[data-l="en"]').click()
        self.assertEqual(self.page.locator("#quit").inner_text(), "Quit")
        self.assertEqual(self.page.locator("#english").input_value(), "3")
        self.page.locator("#quit").click()
        self.page.get_by_text("The app is closed. You can close this tab.").wait_for()
        self.assertEqual(self.page.evaluate("localStorage.getItem('lang')"), "en")
        self.assertEqual(len(self.quit_requests), 1)
        self.assertEqual(self.quit_requests[0].method, "POST")
        self.assertEqual(self.errors, [])

    def test_invalid_saved_language_does_not_break_real_page(self):
        self.context.add_init_script("localStorage.setItem('lang', 'constructor')")
        self.page.goto("http://autoapply.test/")
        self.page.wait_for_function("document.getElementById('english').value === '3'")
        self.assertEqual(self.page.locator("#quit").inner_text(), "Quit")
        self.page.locator('[data-l="tr"]').click()
        self.assertEqual(self.page.evaluate("localStorage.getItem('lang')"), "tr")
        self.assertEqual(self.errors, [])

    def test_turkish_browser_defaults_to_english_and_can_explicitly_switch(self):
        self.context.add_init_script("Object.defineProperty(navigator, 'language', {get: () => 'tr-TR'})")
        self.page.goto('http://autoapply.test/')
        self.page.wait_for_function("document.getElementById('english').value === '3'")
        self.assertEqual(self.page.locator('#quit').inner_text(), 'Quit')
        self.assertEqual(self.page.locator('html').get_attribute('lang'), 'en')
        self.page.locator('[data-l="tr"]').click()
        self.assertEqual(self.page.locator('#quit').inner_text(), 'Kapat')
        self.assertEqual(self.page.locator('html').get_attribute('lang'), 'tr')
        self.assertEqual(self.errors, [])

    def test_invalid_cached_profile_message_is_visible_without_js_error(self):
        self.profile_error = "profile.skills: expected array"
        self.page.goto("http://autoapply.test/")
        self.page.get_by_text("Invalid profile. Check your details or read the CV again.", exact=False).wait_for()
        self.assertIn("profile.skills", self.page.locator("#m1").inner_text())
        self.assertEqual(self.errors, [])

    def test_invalid_profile_response_does_not_start_progress_or_disable_retry(self):
        self.start_error = {"error": "invalid_profile", "detail": "profile.total_years: expected integer"}
        self.page.goto("http://autoapply.test/")
        self.page.wait_for_function("document.getElementById('english').value === '3'")
        self.page.locator("#manual").click()
        self.page.locator("#job_titles").fill("Software Engineer")
        self.page.locator("#total_years").fill("not-a-number")
        self.page.locator("#start").click()
        self.page.locator("#m3").filter(has_text="profile.total_years").wait_for()
        self.assertTrue(self.page.locator("#start").is_enabled())
        self.assertFalse(self.page.locator("#s4").is_visible())
        self.assertEqual(self.errors, [])
