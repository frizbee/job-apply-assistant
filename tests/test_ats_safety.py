"""ATS URL and document-boundary regression tests; no external network requests."""
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_answer_safety import SafetyTest, node
import apply


class URLTests(SafetyTest):
    def test_only_exact_approved_https_hosts_identify_providers(self):
        for provider, hosts in apply.ATS.items():
            for host in hosts:
                with self.subTest(host=host):
                    self.assertEqual(apply.ats_provider(f"https://{host}/company/job"), provider)
                    self.assertEqual(apply.ats_provider(f"https://{host.upper()}:443/company/job"), provider)

    def test_spoofed_malformed_and_non_https_urls_are_not_trusted(self):
        urls = [
            "https://attacker.example/greenhouse.io/form",
            "https://attacker.example/?next=https://jobs.lever.co/company/job",
            "https://jobs.lever.co.attacker.example/job", "https://eviljobs.lever.co/job",
            "https://jobs.ashbyhq.com.attacker.example/job",
            "https://job-boards.greenhouse.io.attacker.example/job",
            "https://boards.greenhouse.io@attacker.example/job",
            "https://attacker.example@jobs.lever.co/job",
            "https://user:password@jobs.lever.co/job", "https://jobs.lever.co:8443/job",
            "http://jobs.lever.co/job", "//jobs.lever.co/job", "javascript:alert(1)",
            "file:///jobs.lever.co/job", "https://jobs.lever.co./job",
            "https://jobs%2elever.co/job", "https://jobs.lever.co\\@attacker.example/job",
            "https://jobs.lever.co\n.attacker.example/job", " https://jobs.lever.co/job",
            "https://[invalid/job", "https://jobs.lever.co:invalid/job", "",
        ]
        for url in urls:
            with self.subTest(url=url):
                self.assertIsNone(apply.ats_provider(url))

    def test_application_suffix_uses_path_not_query_or_fragment(self):
        page = node()
        cases = [
            ("https://jobs.lever.co/company/id?next=/apply#details",
             "https://jobs.lever.co/company/id/apply?next=/apply#details", "lever"),
            ("https://jobs.lever.co/company/id/apply/?source=x",
             "https://jobs.lever.co/company/id/apply?source=x", "lever"),
            ("https://jobs.ashbyhq.com/company/id?next=/application",
             "https://jobs.ashbyhq.com/company/id/application?next=/application", "ashby"),
            ("https://job-boards.greenhouse.io/company/jobs/123?gh_src=x",
             "https://job-boards.greenhouse.io/company/jobs/123?gh_src=x", "greenhouse"),
        ]
        for url, expected, provider in cases:
            self.assertEqual(apply.ats_apply_url(page, url), (expected, provider))
        page.goto.assert_not_called()

    def test_company_iframe_discovery_ignores_fake_hosts_and_paths(self):
        page = node()
        page.url = "https://company.example/careers"
        page.frames = [SimpleNamespace(url=url) for url in [
            page.url, "https://evil.example/greenhouse.io/form",
            "https://jobs.lever.co.evil.example/job", "http://jobs.lever.co/company/id",
            "https://jobs.ashbyhq.com/company/id",
        ]]
        self.assertEqual(apply.ats_apply_url(page, page.url),
                         ("https://jobs.ashbyhq.com/company/id/application", "ashby"))

    def test_company_redirect_to_trusted_ats_is_supported(self):
        page = node()
        page.url = "https://jobs.eu.lever.co/company/id"
        page.frames = []
        self.assertEqual(apply.ats_apply_url(page, "https://company.example/careers"),
                         ("https://jobs.eu.lever.co/company/id/apply", "lever"))

    def test_invalid_discovery_urls_are_not_opened(self):
        page = node()
        for url in ["http://company.example/jobs", "file:///private/careers", "https://company.example:bad/jobs"]:
            self.assertEqual(apply.ats_apply_url(page, url), (None, "unknown"))
        page.goto.assert_not_called()


class ExternalBoundaryTests(SafetyTest):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.object(apply, "EXTERNAL", True))
        self.enterContext(patch.object(apply, "EXT_STATE", {"count": 0, "last": 0.0}))
        self.enterContext(patch.object(apply, "DRY_RUN", False))
        self.manual = self.enterContext(patch.object(apply, "record_manual"))
        self.fill = self.enterContext(patch.object(apply, "fill_step", return_value=[]))
        self.page, self.form = node(), node()
        self.page.locator.return_value = self.form
        self.page.evaluate.return_value = True
        self.page.url = "https://jobs.lever.co/company/id/apply"
        self.page.goto.side_effect = lambda url, **kwargs: setattr(self.page, "url", url)

    def run_form(self):
        return apply.apply_form(self.page, "test", "Engineer", "Example", "https://jobs.lever.co/company/id")

    def test_redirect_to_untrusted_site_blocks_all_data_entry(self):
        for url in ["https://evil.example/form", "http://jobs.lever.co/company/id/apply",
                    "https://jobs.ashbyhq.com/other/company"]:
            with self.subTest(url=url):
                self.page.goto.side_effect = lambda *args, url=url, **kwargs: setattr(self.page, "url", url)
                self.assertEqual(self.run_form(), "external_manual")
                self.fill.assert_not_called()
                self.form.click.assert_not_called()

    def test_untrusted_native_form_target_blocks_before_filling(self):
        self.form.evaluate.return_value = ["https://evil.example/collect"]
        self.assertEqual(self.run_form(), "external_manual")
        self.fill.assert_not_called()
        self.manual.assert_called_once()

    def test_navigation_after_first_pass_prevents_second_pass_and_submission(self):
        def navigate(*args, **kwargs):
            self.page.url = "https://evil.example/form"
            return []
        self.fill.side_effect = navigate
        self.assertEqual(self.run_form(), "external_manual")
        self.assertEqual(self.fill.call_count, 1)
        self.form.click.assert_not_called()

    def test_changed_form_action_is_rechecked(self):
        self.form.evaluate.side_effect = [[], ["https://evil.example/collect"]]
        self.assertEqual(self.run_form(), "external_manual")
        self.assertEqual(self.fill.call_count, 1)
        self.form.click.assert_not_called()

    def test_document_race_before_marker_blocks_data_entry(self):
        self.page.evaluate.return_value = False
        self.assertEqual(self.run_form(), "external_manual")
        self.fill.assert_not_called()

    def test_failed_form_inspection_blocks_data_entry(self):
        self.form.evaluate.side_effect = RuntimeError("document replaced")
        self.assertEqual(self.run_form(), "external_manual")
        self.fill.assert_not_called()


@unittest.skipUnless(os.getenv("AUTOAPPLY_BROWSER_TESTS") == "1", "optional local Chrome DOM tests")
class BrowserBoundaryTests(SafetyTest):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.pw = sync_playwright().start()
        cls.addClassCleanup(cls.pw.stop)
        cls.browser = cls.pw.chromium.launch(channel="chrome", headless=True)
        cls.addClassCleanup(cls.browser.close)

    def setUp(self):
        super().setUp()
        self.context = self.browser.new_context()
        self.addCleanup(self.context.close)
        self.html = '<form><label for="email">Email</label><input id="email" type="email"><input type="file" name="resume"><button type="submit">Submit</button></form>'
        # Every request is served locally, including fake ATS and attacker origins.
        self.context.route("**/*", lambda route: route.fulfill(content_type="text/html", body=self.html))
        self.page = self.context.new_page()
        self.page.set_default_timeout(300)
        self.enterContext(patch.object(apply, "record_manual"))

    def verified_form(self):
        self.page.goto("https://jobs.lever.co/company/id/apply")
        document = apply.trusted_ats_document(self.page, "lever")
        return document, document.locator("form")

    def test_verified_document_allows_filling_and_resume_upload(self):
        document, form = self.verified_form()
        apply.check_ats_form(self.page, document, form, "lever")
        form.locator("#email").fill("test@example.com")
        form.locator('input[type="file"]').set_input_files(
            {"name": "resume.pdf", "mimeType": "application/pdf", "buffer": b"test resume"})
        self.assertEqual(form.locator("#email").input_value(), "test@example.com")
        self.assertEqual(form.locator('input[type="file"]').evaluate("e => e.files[0].name"), "resume.pdf")

    def test_locators_cannot_fill_or_upload_after_cross_origin_navigation(self):
        from playwright.sync_api import TimeoutError
        document, form = self.verified_form()
        self.page.goto("https://evil.example/form")
        with self.assertRaises(apply.UntrustedApplicationSite):
            apply.check_ats_form(self.page, document, form, "lever")
        with self.assertRaises(TimeoutError):
            form.locator("#email").fill("private@example.com")
        with self.assertRaises(TimeoutError):
            form.locator('input[type="file"]').set_input_files(
                {"name": "resume.pdf", "mimeType": "application/pdf", "buffer": b"private resume"})
        self.assertEqual(self.page.locator("#email").input_value(), "")
        self.assertEqual(self.page.locator('input[type="file"]').evaluate("e => e.files.length"), 0)

    def test_same_host_new_document_does_not_inherit_marker(self):
        document, form = self.verified_form()
        self.page.reload()
        self.assertEqual(document.count(), 0)
        with self.assertRaises(apply.UntrustedApplicationSite):
            apply.check_ats_form(self.page, document, form, "lever")

    def test_native_action_and_submitter_override_are_checked(self):
        document, form = self.verified_form()
        for attr in ["action", "formaction"]:
            element = form if attr == "action" else form.locator("button")
            element.evaluate("(e, attr) => e.setAttribute(attr, 'https://evil.example/collect')", attr)
            with self.assertRaises(apply.UntrustedApplicationSite):
                apply.check_ats_form(self.page, document, form, "lever")
            element.evaluate("(e, attr) => e.removeAttribute(attr)", attr)

    def test_http_redirect_is_checked_before_data_entry(self):
        self.context.unroute("**/*")
        def respond(route):
            if "jobs.lever.co" in route.request.url:
                route.fulfill(status=302, headers={"Location": "https://evil.example/form"})
            else:
                route.fulfill(content_type="text/html", body=self.html)
        self.context.route("**/*", respond)
        self.page.goto("https://jobs.lever.co/company/id/apply")
        with self.assertRaises(apply.UntrustedApplicationSite):
            apply.trusted_ats_document(self.page, "lever")
        self.assertEqual(self.page.locator("#email").input_value(), "")


if __name__ == "__main__":
    unittest.main()
