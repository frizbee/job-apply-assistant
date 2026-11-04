"""Buk email verification is manual navigation, never final-send authorization."""
import os
import unittest

import deferred_review
from manual_browser_steps import BUK_EMAIL_STEP_JS


@unittest.skipUnless(os.getenv('AUTOAPPLY_BROWSER_TESTS') == '1', 'optional local Chrome DOM tests')
class BukEmailStepTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.pw = sync_playwright().start()
        cls.addClassCleanup(cls.pw.stop)
        cls.browser = cls.pw.chromium.launch(channel='chrome', headless=True)
        cls.addClassCleanup(cls.browser.close)

    def setUp(self):
        self.context = self.browser.new_context()
        self.addCleanup(self.context.close)
        self.html = '''<form id="invisible-recaptcha-form" method="post" action="postular/verificar">
            <input id="login_form_email" name="login_form[email]" type="email" required>
            <input id="login_form_accept_terms" name="login_form[accept_terms]" type="checkbox">
            <input type="hidden" name="authenticity_token" value="local-fixture">
            <button type="submit">Postular</button></form>
            <script>window.attempts=0;document.querySelector('form').addEventListener('submit',e=>{
                e.preventDefault();window.attempts++;
            });</script>'''
        self.context.route('**/*', lambda route: route.fulfill(content_type='text/html', body=self.html))
        self.page = self.context.new_page()
        self.page.add_init_script(deferred_review.GUARD_JS)
        self.url = 'https://buk.buk.cl/seleccions/' + 'a' * 64 + '/postular'

    def load(self, checked=True):
        self.page.goto(self.url)
        self.assertTrue(self.page.evaluate(BUK_EMAIL_STEP_JS))
        self.page.locator('#login_form_email').fill('test@example.org')
        if checked:
            self.page.locator('#login_form_accept_terms').check()

    def test_user_click_can_verify_email_without_changing_consent(self):
        self.load()
        self.page.get_by_role('button', name='Postular').click()
        self.assertEqual(self.page.evaluate('window.attempts'), 1)
        self.assertTrue(self.page.locator('#login_form_accept_terms').is_checked())
        self.page.wait_for_timeout(20)
        self.assertFalse(self.page.evaluate('window.__autoapplyManualAllowSubmit'))

    def test_unchecked_terms_are_never_inferred(self):
        self.load(checked=False)
        self.page.get_by_role('button', name='Postular').click()
        self.assertEqual(self.page.evaluate('window.attempts'), 0)
        self.assertFalse(self.page.locator('#login_form_accept_terms').is_checked())

    def test_other_submit_targets_remain_blocked(self):
        self.load()
        self.page.evaluate("document.querySelector('form').action='/applications/submit'")
        self.page.get_by_role('button', name='Postular').click()
        self.assertEqual(self.page.evaluate('window.attempts'), 0)

    def test_full_application_fields_cannot_use_email_exemption(self):
        self.html = self.html.replace('<button', '<input type="file" name="resume"><button')
        self.load()
        self.page.get_by_role('button', name='Postular').click()
        self.assertEqual(self.page.evaluate('window.attempts'), 0)

    def test_synthetic_page_click_cannot_authorize_verification(self):
        self.load()
        self.page.evaluate("document.querySelector('button').click()")
        self.assertEqual(self.page.evaluate('window.attempts'), 0)

    def test_one_authentication_step_does_not_authorize_final_application(self):
        self.load()
        self.page.get_by_role('button', name='Postular').click()
        self.page.wait_for_timeout(20)
        self.page.evaluate("document.querySelector('form').action='/applications/submit'")
        self.page.get_by_role('button', name='Postular').click()
        self.assertEqual(self.page.evaluate('window.attempts'), 1)

    def test_submission_guard_does_not_interfere_with_manual_captcha_frame(self):
        self.context.unroute('**/*')
        def route(request):
            html = ('<button type="submit">Local fixture only</button>' if '/recaptcha/api2/bframe' in request.request.url
                    else '<iframe src="https://www.recaptcha.net/recaptcha/api2/bframe"></iframe>')
            request.fulfill(content_type='text/html', body=html)
        self.context.route('**/*', route)
        self.page.goto('https://example.org/fixture')
        frame = next(frame for frame in self.page.frames if '/recaptcha/api2/bframe' in frame.url)
        self.assertEqual(frame.evaluate('typeof window.__autoapplyManualAllowSubmit'), 'undefined')
        self.assertFalse(self.page.evaluate('window.__autoapplyManualAllowSubmit'))

    def test_other_nested_forms_and_top_level_captcha_urls_remain_guarded(self):
        self.context.unroute('**/*')
        self.context.route('**/*', lambda route: route.fulfill(content_type='text/html', body='<p>Local fixture</p>'))
        self.page.goto('https://www.recaptcha.net/recaptcha/api2/bframe')
        self.assertFalse(self.page.evaluate('window.__autoapplyManualAllowSubmit'))
