"""Indeed login/challenge waits must not advance queries or touch credentials."""
import os
import unittest
from unittest.mock import MagicMock, patch

import apply
import sources
from test_answer_safety import SafetyTest, node


class IndeedWaitTests(SafetyTest):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.object(sources, 'core', apply))

    def test_auth_urls_cover_more_than_secure_auth(self):
        page = node()
        for url in ['https://secure.indeed.com/auth', 'https://accounts.indeed.com/',
                    'https://auth.indeed.com/verify',
                    'https://www.indeed.com/account/login', 'https://www.indeed.com/login',
                     'https://secure.indeed.com/account/verify',
                     'https://secure.indeed.com/oauth/v2/authorize',
                     'https://authentication-session-manager.indeed.com/bff/oauth/callback']:
            page.url = url
            self.assertTrue(sources.indeed_auth_pending(page), url)

    def test_search_stops_on_unfinished_login_without_loading_next_keyword(self):
        page = node()
        apply.CFG['search']['keywords'] = ['First query', 'Second query']
        with patch.object(sources, 'indeed_wait_ready', return_value=False) as wait:
            self.assertEqual(list(sources.indeed(page)), [])
        page.goto.assert_called_once()
        self.assertIn('First+query', page.goto.call_args.args[0])
        self.assertEqual(wait.call_args.args, (page, 'search'))
        self.assertEqual(wait.call_args.kwargs['expected_url'], page.goto.call_args.args[0])
        self.assertIn('search_state', wait.call_args.kwargs)
        page.reload.assert_not_called()

    def test_shared_navigation_only_adds_wait_for_indeed(self):
        page = node()
        with patch.object(sources, 'indeed_wait_ready', return_value=True) as indeed_wait, \
             patch.object(apply, 'wait_human', return_value=True) as normal_wait:
            self.assertTrue(sources.goto(page, 'https://wellfound.com/remote', 'Wellfound'))
            indeed_wait.assert_not_called()
            normal_wait.assert_called_once()
            normal_wait.reset_mock()
            self.assertTrue(sources.goto(page, 'https://www.indeed.com/viewjob?jk=1', 'indeed'))
            indeed_wait.assert_called_once_with(page, 'detail')
            normal_wait.assert_not_called()

    def test_application_wait_blocks_filling_during_login(self):
        page = node()
        page.url = 'https://secure.indeed.com/auth'
        page.context.pages = [page]
        job = {'id': 'indeed:job', 'url': 'https://www.indeed.com/viewjob?jk=job', 'title': 'Engineer', 'company': 'Example'}
        with patch.object(sources, 'goto', return_value=True), \
             patch.object(apply, 'click_form_control'), patch.object(apply, 'fill_step') as fill, \
             patch.object(apply, 'wait_human', return_value=False):
            self.assertEqual(sources.indeed_apply(page, job, {}), 'blocked')
        fill.assert_not_called()
        page.reload.assert_not_called()


@unittest.skipUnless(os.getenv('AUTOAPPLY_BROWSER_TESTS') == '1', 'offline Chrome login fixtures')
class IndeedLoginBrowserTests(SafetyTest):
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
        self.login_complete = False
        self.requests = []
        def route(r):
            self.requests.append(r.request.url)
            if '/jobs' in r.request.url and not self.login_complete:
                r.fulfill(content_type='text/html', body="<script>location.href='https://accounts.indeed.com/auth'</script>")
            elif '/auth' in r.request.url:
                r.fulfill(content_type='text/html', body='''<h1>Sign in or create an account</h1>
                    <label for="email">Email</label><input id="email" autocomplete="username">
                    <button type="button" onclick="location.href='https://www.indeed.com/jobs?q=First'">Complete login</button>''')
            else:
                r.fulfill(content_type='text/html', body='<div data-jk="123">Senior Developer<br>Example</div>')
        self.ctx.route('**/*', route)

    def test_login_input_survives_multiple_checks_and_original_query_waits(self):
        apply.CFG['search']['keywords'] = ['First', 'Second']
        def wait(page, check, what, minutes):
            page.wait_for_url('https://accounts.indeed.com/auth')
            self.assertFalse(check(page))
            self.assertIn('/auth', page.url, str(self.requests))
            self.assertGreater(page.locator('#email').count(), 0, page.content()[:1000])
            page.get_by_label('Email').fill('user@example.test')
            for _ in range(3):
                self.assertFalse(check(page))
                self.assertEqual(page.get_by_label('Email').input_value(), 'user@example.test')
            self.assertEqual(len([u for u in self.requests if '/jobs' in u]), 1)
            self.login_complete = True
            page.get_by_role('button', name='Complete login').click()
            page.wait_for_url('https://www.indeed.com/jobs?q=First')
            self.assertFalse(check(page))  # Positive results need a stable interval.
            page.wait_for_timeout(1100)
            self.assertTrue(check(page))
            return True
        with patch.object(apply, 'wait_human', side_effect=wait), \
             patch.object(self.page, 'goto', wraps=self.page.goto) as goto, \
             patch.object(self.page, 'reload', wraps=self.page.reload) as reload:
            generator = sources.indeed(self.page)
            job, callback = next(generator)
            generator.close()
        self.assertEqual(job['id'], 'indeed:123')
        goto.assert_called_once()
        reload.assert_not_called()
        self.assertFalse(any('Second' in u for u in self.requests))

    def test_blank_redirect_document_is_not_search_readiness(self):
        self.login_complete = True
        self.page.goto('https://www.indeed.com/jobs')
        self.page.set_content('<body></body>')
        self.assertFalse(sources.indeed_ready_evidence(self.page, 'search'))
        self.page.set_content('<div class="jobsearch-NoResult-messageContainer">No jobs found</div>')
        self.assertTrue(sources.indeed_ready_evidence(self.page, 'search'))

    def test_legacy_predicate_accepts_login_but_current_wait_does_not(self):
        self.page.goto('https://accounts.indeed.com/auth')
        self.assertTrue(sources.page_ready(self.page))  # Identical to pinned upstream page_ready.
        self.assertFalse(sources.indeed_ready_evidence(self.page, 'search'))

    def test_legacy_predicate_accepts_homepage_without_search_cards(self):
        self.login_complete = True
        self.page.goto('https://www.indeed.com/')
        self.page.set_content('<header>Indeed home</header>')
        self.assertTrue(sources.page_ready(self.page))
        self.assertFalse(sources.indeed_ready_evidence(self.page, 'search'))

    def test_both_predicates_reject_cloudflare_and_accept_results(self):
        self.login_complete = True
        self.page.goto('https://www.indeed.com/jobs')
        self.assertTrue(sources.page_ready(self.page))
        self.assertTrue(sources.indeed_ready_evidence(self.page, 'search'))
        self.page.set_content('<title>Just a moment...</title><p>Verify you are human</p>')
        self.assertFalse(sources.page_ready(self.page))
        self.assertFalse(sources.indeed_ready_evidence(self.page, 'search'))

    def test_login_overlay_blocks_cards_and_application_continue_buttons(self):
        self.login_complete = True
        self.page.goto('https://www.indeed.com/jobs')
        self.page.set_content('<div data-jk="123">Job</div><h1>Sign in</h1><input type="password"><button>Continue</button>')
        self.assertFalse(sources.indeed_ready_evidence(self.page, 'search'))
        self.assertFalse(sources.indeed_ready_evidence(self.page, 'application'))

    def test_verification_failure_does_not_advance_to_next_search(self):
        apply.CFG['search']['keywords'] = ['First', 'Second']
        with patch.object(apply, 'wait_human', return_value=False), \
             patch.object(self.page, 'goto', wraps=self.page.goto) as goto:
            self.assertEqual(list(sources.indeed(self.page)), [])
        goto.assert_called_once()
