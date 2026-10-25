"""One exact search return after verified sign-in; never a CAPTCHA retry loop."""
import os
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlparse

import apply
import sources
from test_answer_safety import SafetyTest


@unittest.skipUnless(os.getenv('AUTOAPPLY_BROWSER_TESTS') == '1', 'local Indeed resume fixtures')
class IndeedSearchResumeTests(SafetyTest):
    TARGET = 'https://www.indeed.com/jobs?q=Senior+Ruby&l=Remote&fromage=7&sort=date'

    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.pw = sync_playwright().start()
        cls.addClassCleanup(cls.pw.stop)
        cls.browser = cls.pw.chromium.launch(channel='chrome', headless=True)
        cls.addClassCleanup(cls.browser.close)

    def setUp(self):
        super().setUp()
        self.ctx = self.browser.new_context()
        self.addCleanup(self.ctx.close)
        self.page = self.ctx.new_page()
        self.page.set_default_timeout(2000)
        self.enterContext(patch.object(sources, 'core', apply))
        self.enterContext(patch.object(apply, 'STOP', threading.Event()))
        self.enterContext(patch.object(apply, 'DEADLINE', float('inf')))
        self.home_kind, self.after_resume = 'signed_in', 'cards'
        self.via_oauth, self.home_status = False, None
        self.search_requests, self.all_requests = [], []
        def respond(route):
            path = urlparse(route.request.url).path
            self.all_requests.append(route.request.url)
            if path == '/jobs':
                self.search_requests.append(route.request.url)
                if len(self.search_requests) == 1 or self.after_resume == 'login':
                    route.fulfill(content_type='text/html', body="<script>location.href='https://secure.indeed.com/auth'</script>")
                elif self.after_resume == 'home':
                    route.fulfill(content_type='text/html', body="<script>location.href='https://www.indeed.com/'</script>")
                elif self.after_resume == 'challenge':
                    route.fulfill(status=403, content_type='text/html', body='<title>Just a moment...</title><p>Verify you are human</p>')
                elif self.after_resume == 'empty':
                    route.fulfill(content_type='text/html', body='<div data-testid="no-results">No jobs found</div>')
                else:
                    route.fulfill(content_type='text/html', body='<div data-jk="job-1">Ruby Engineer<br>Example</div>')
            elif path == '/auth':
                destination = 'https://authentication-session-manager.indeed.com/bff/oauth/callback' if self.via_oauth else 'https://www.indeed.com/'
                route.fulfill(content_type='text/html', body=f'''<h1>Sign in</h1><input autocomplete="username" aria-label="Email">
                    <button type="button" onclick="location.href='{destination}'">Complete login</button>''')
            elif path == '/bff/oauth/callback':
                # A fulfilled HTTP redirect can bypass a second Playwright route
                # on older versions. A fresh JS navigation keeps this fixture
                # wholly offline while exercising the same callback->home state.
                route.fulfill(content_type='text/html', body="<script>location.href='https://www.indeed.com/'</script>")
            elif path == '/telemetry':
                route.fulfill(status=403, content_type='text/plain', body='Forbidden')
            else:
                body = '<main>Indeed home</main>'
                status = 200
                if self.home_kind in ('signed_in', 'forbidden', 'signed_in_with_login'):
                    body += '<button id="AccountMenu-button">Account</button>'
                if self.home_kind == 'signed_in_with_login':
                    body += '<a href="/auth">Sign in</a>'
                if self.home_kind == 'blank':
                    body = '<body></body>'
                if self.home_kind == 'challenge':
                    body = '<title>Just a moment...</title><p>Verify you are human</p>'
                if self.home_kind == 'forbidden':
                    status = 403  # Even a 403 with normal account chrome must not trigger a retry.
                route.fulfill(status=self.home_status or status, content_type='text/html', body=body)
        self.ctx.route('**/*', respond)

    def login_to_home(self, page, check):
        page.wait_for_url('https://secure.indeed.com/auth')
        self.assertFalse(check(page))
        page.get_by_label('Email').fill('reviewed@example.test')
        self.assertFalse(check(page))
        self.assertEqual(page.get_by_label('Email').input_value(), 'reviewed@example.test')
        self.assertEqual(len(self.search_requests), 1)
        page.get_by_role('button', name='Complete login').click()
        page.wait_for_url('https://www.indeed.com/')
        page.wait_for_load_state('domcontentloaded')
        self.assertIn('https://www.indeed.com/', self.all_requests)
        self.assertFalse(check(page))  # Do not act on a transient homepage.
        self.assertEqual(len(self.search_requests), 1)
        page.wait_for_timeout(1100)

    def test_verified_home_resumes_exact_search_in_same_tab_then_requires_stable_results(self):
        def wait(page, check, what, minutes):
            self.login_to_home(page, check)
            self.assertFalse(check(page))  # Performs the one allowed return.
            self.assertEqual(self.search_requests, [self.TARGET, self.TARGET])
            self.assertFalse(check(page))
            page.wait_for_timeout(1100)
            self.assertTrue(check(page))
            return True
        with patch.object(apply, 'wait_human', side_effect=wait), patch.object(self.page, 'reload', wraps=self.page.reload) as reload:
            self.assertTrue(sources.goto(self.page, self.TARGET, 'Indeed'))
        self.assertEqual(len(self.ctx.pages), 1)
        reload.assert_not_called()

    def test_homepage_loop_gets_only_one_return_and_no_next_keyword(self):
        self.after_resume = 'home'
        apply.CFG['search']['keywords'] = ['First', 'Second']
        def wait(page, check, what, minutes):
            self.login_to_home(page, check)
            self.assertFalse(check(page))
            page.wait_for_url('https://www.indeed.com/')
            for _ in range(3):
                self.assertFalse(check(page))
            self.assertEqual(len(self.search_requests), 2)
            return False
        with patch.object(apply, 'wait_human', side_effect=wait):
            self.assertEqual(list(sources.indeed(self.page)), [])
        self.assertTrue(all('q=First' in url for url in self.search_requests))

    def test_signed_out_blank_unknown_or_http403_home_does_not_resume(self):
        for kind in ('unknown', 'blank', 'signed_in_with_login', 'forbidden', 'challenge'):
            with self.subTest(kind=kind):
                self.home_kind = kind
                self.search_requests.clear()
                def wait(page, check, what, minutes):
                    self.login_to_home(page, check)
                    self.assertFalse(check(page))
                    self.assertEqual(len(self.search_requests), 1)
                    return False
                with patch.object(apply, 'wait_human', side_effect=wait):
                    self.assertFalse(sources.goto(self.page, self.TARGET, 'Indeed'))

    def test_challenge_or_login_after_return_stops_without_another_login_wait(self):
        for mode in ('challenge', 'login'):
            with self.subTest(mode=mode):
                self.after_resume = mode
                self.search_requests.clear()
                reached = []
                def wait(page, check, what, minutes):
                    self.login_to_home(page, check)
                    self.assertFalse(check(page))
                    if mode == 'login':
                        page.wait_for_url('https://secure.indeed.com/auth')
                    reached.append(True)
                    check(page)  # Interrupts the wait; does not ask the user to repeat login.
                    self.fail('The resumed search should stop on login/security')
                with patch.object(apply, 'wait_human', side_effect=wait) as waiting:
                    self.assertFalse(sources.goto(self.page, self.TARGET, 'Indeed'))
                waiting.assert_called_once()
                self.assertTrue(reached)
                self.assertEqual(len(self.search_requests), 2)

    def test_stop_and_deadline_before_return_prevent_navigation(self):
        for stop in (True, False):
            with self.subTest(stop=stop):
                self.search_requests.clear()
                apply.STOP.clear()
                apply.DEADLINE = float('inf')
                def wait(page, check, what, minutes):
                    self.login_to_home(page, check)
                    if stop:
                        apply.STOP.set()
                    else:
                        apply.DEADLINE = time.time() - 1
                    return check(page)
                with patch.object(apply, 'wait_human', side_effect=wait):
                    with self.assertRaises(apply.Stopped if stop else apply.LimitReached):
                        sources.goto(self.page, self.TARGET, 'Indeed')
                self.assertEqual(len(self.search_requests), 1)

    def test_invalid_target_never_navigates(self):
        for target in ('https://evil.test/jobs?q=Ruby', 'http://www.indeed.com/jobs?q=Ruby',
                       'https://www.indeed.com/jobs', 'https://user:password@www.indeed.com/jobs?q=Ruby'):
            with self.subTest(target=target), patch.object(self.page, 'goto', wraps=self.page.goto) as goto:
                with self.assertRaises(ValueError):
                    sources.goto(self.page, target, 'Indeed')
                goto.assert_not_called()

    def test_search_response_listener_removed_even_on_failure(self):
        with patch.object(apply, 'wait_human', return_value=False), \
             patch.object(self.page, 'remove_listener', wraps=self.page.remove_listener) as remove:
            self.assertFalse(sources.goto(self.page, self.TARGET, 'Indeed'))
        remove.assert_called_once()
        self.assertEqual(remove.call_args.args[0], 'response')

    def test_explicit_empty_results_after_return_are_ready(self):
        self.after_resume = 'empty'
        def wait(page, check, what, minutes):
            self.login_to_home(page, check)
            self.assertFalse(check(page))
            self.assertFalse(check(page))
            page.wait_for_timeout(1100)
            return check(page)
        with patch.object(apply, 'wait_human', side_effect=wait):
            self.assertTrue(sources.goto(self.page, self.TARGET, 'Indeed'))
        self.assertEqual(self.search_requests, [self.TARGET, self.TARGET])

    def test_new_home_document_restarts_stability_timer(self):
        def wait(page, check, what, minutes):
            self.login_to_home(page, check)
            page.reload(wait_until='domcontentloaded')  # Simulate the user's refresh, not an app retry.
            self.assertFalse(check(page))
            self.assertEqual(len(self.search_requests), 1)
            page.wait_for_timeout(1100)
            self.assertFalse(check(page))
            self.assertEqual(len(self.search_requests), 2)
            return False
        with patch.object(apply, 'wait_human', side_effect=wait):
            self.assertFalse(sources.goto(self.page, self.TARGET, 'Indeed'))

    def test_detail_and_application_waits_never_return_to_search(self):
        for stage in ('detail', 'application'):
            with self.subTest(stage=stage):
                self.page.goto('https://www.indeed.com/')
                state = {'document_status': 200, 'document': ('https', 'www.indeed.com', '/')}
                def wait(page, check, what, minutes):
                    self.assertFalse(check(page))
                    page.wait_for_timeout(1100)
                    return check(page)
                with patch.object(apply, 'wait_human', side_effect=wait), patch.object(self.page, 'goto', wraps=self.page.goto) as goto:
                    self.assertFalse(sources.indeed_wait_ready(self.page, stage, expected_url=self.TARGET, search_state=state))
                goto.assert_not_called()

    def test_oauth_return_denied_stops_without_another_search_or_login_wait(self):
        self.via_oauth = True
        for status in (401, 403, 429):
            with self.subTest(status=status):
                self.home_status = status
                self.search_requests.clear()
                reached = []
                def wait(page, check, what, minutes):
                    page.wait_for_url('https://secure.indeed.com/auth')
                    self.assertFalse(check(page))
                    page.get_by_role('button', name='Complete login').click()
                    page.wait_for_url('https://www.indeed.com/')
                    page.wait_for_load_state('domcontentloaded')
                    self.assertIn('https://www.indeed.com/', self.all_requests)
                    reached.append(True)
                    check(page)
                    self.fail('An OAuth access denial must terminate the wait')
                with patch.object(apply, 'wait_human', side_effect=wait) as waiting:
                    self.assertFalse(sources.goto(self.page, self.TARGET, 'Indeed'))
                self.assertTrue(reached)
                waiting.assert_called_once()
                self.assertEqual(self.search_requests, [self.TARGET])

    def test_telemetry_403_after_oauth_does_not_become_document_access_denial(self):
        self.via_oauth = True
        def wait(page, check, what, minutes):
            self.login_to_home(page, check)
            self.assertEqual(page.evaluate("fetch('/telemetry').then(r=>r.status)"), 403)
            self.assertFalse(check(page))
            self.assertFalse(check(page))
            page.wait_for_timeout(1100)
            return check(page)
        with patch.object(apply, 'wait_human', side_effect=wait):
            self.assertTrue(sources.goto(self.page, self.TARGET, 'Indeed'))
        self.assertEqual(self.search_requests, [self.TARGET, self.TARGET])


if __name__ == '__main__':
    unittest.main()
