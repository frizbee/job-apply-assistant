"""Remote OK detail-first filtering and current-job-only Apply routing."""
import copy
import datetime as dt
import html
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import apply
import sources
from platforms.remoteok import RemoteOKAdapter, complete_description, combine_descriptions
from test_answer_safety import SafetyTest


class DescriptionTests(unittest.TestCase):
    def test_board_boilerplate_does_not_make_a_truncated_description_complete(self):
        self.assertFalse(complete_description('Build systems... Please mention the word BLUE when applying.'))
        self.assertFalse(complete_description('Build systems…'))
        self.assertFalse(complete_description('Build systemsâ€¦'))
        self.assertFalse(complete_description('Build systems... Apply now and work remotely at Example'))
        self.assertFalse(complete_description(''))
        self.assertTrue(complete_description('Full employer description.'))

    def test_combination_preserves_instructions_without_duplicate_substrings(self):
        self.assertEqual(combine_descriptions('Full text.', 'Full text. Please mention BLUE.'),
                         'Full text. Please mention BLUE.')
        self.assertIn('Full description.', combine_descriptions('Full description.', 'Short text. Mention BLUE.'))
        self.assertIn('Mention BLUE', combine_descriptions('Full description.', 'Short text. Mention BLUE.'))


@unittest.skipUnless(os.getenv('AUTOAPPLY_BROWSER_TESTS') == '1', 'offline Remote OK detail fixtures')
class RemoteOKDetailTests(SafetyTest):
    URL = 'https://remoteok.com/remote-jobs/remote-senior-backend-engineer-example-123'

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
        self.page.set_default_timeout(1500)
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        for field in ('APPLIED_FILE', 'LOG_FILE', 'MANUAL_FILE'):
            self.enterContext(patch.object(apply, field, self.home / field))
        self.enterContext(patch.object(apply, 'STOP', threading.Event()))
        self.enterContext(patch.object(apply, 'DEADLINE', float('inf')))
        self.enterContext(patch.object(apply, 'DRY_RUN', True))
        self.enterContext(patch.object(sources, 'core', apply))
        self.enterContext(patch.object(sources, 'keyword_match', return_value=True))
        self.enterContext(patch.object(sources.time, 'sleep'))
        self.enterContext(patch.object(sources, 'SOURCES', {'remoteok': sources.remoteok}))
        self.follow = self.enterContext(patch.object(apply, 'follow_apply', return_value='https://jobs.lever.co/example/approved-id'))
        self.external = self.enterContext(patch.object(sources, 'external', return_value='dry_run'))
        self.global_apply = self.enterContext(patch.object(sources, 'click_apply'))
        apply.CFG['sources']['remoteok'] = True
        apply.CFG['search'].update(keywords=['Backend Engineer'], remote_only=True, posted_within_hours=168)
        apply.CFG['limits'].update(max_applications_per_run=5, per_source=5)
        self.native = 'Full employer description. Ruby services are required.'
        self.structured = None
        self.api_desc = 'Short feed summary.'
        self.workplace = 'TELECOMMUTE'
        self.links = [('/l/123', '123', False)]
        self.expanded = True
        self.epoch = dt.datetime.now(dt.timezone.utc).timestamp() - 3600
        self.feed_url = self.URL
        self.enterContext(patch.object(sources, 'http', side_effect=lambda _url: json.dumps([
            {'legal': 'terms'}, {'id': '123', 'position': 'Senior Backend Engineer', 'company': 'Example',
             'url': self.feed_url, 'description': self.api_desc, 'epoch': self.epoch, 'apply_url': 'https://evil.test/wrong-job'}])))
        self.ctx.route('**/*', lambda route: route.fulfill(content_type='text/html', body=self.fixture()))

    def fixture(self):
        posting = {'@type': 'JobPosting', 'title': 'Senior Backend Engineer', 'hiringOrganization': {'name': 'Example'},
                   'datePosted': dt.datetime.fromtimestamp(self.epoch, dt.timezone.utc).isoformat(),
                   'jobLocationType': self.workplace, 'description': self.native if self.structured is None else self.structured}
        row = '<tr class="job" id="job-123" data-id="123" data-href="' + self.URL + '"><td>' + \
              '<h2 itemprop="title">Senior Backend Engineer</h2><script type="application/ld+json">' + json.dumps(posting) + '</script></td></tr>'
        detail = '<tr class="expand" data-id="123"><td><div class="description"><div class="html" id="native">' + html.escape(self.native) + '</div>'
        for href, jid, disabled in self.links:
            detail += f'<a class="action-apply" data-job-id="{jid}" href="{html.escape(href, quote=True)}" aria-disabled="{str(disabled).lower()}">Apply for this job</a>'
        detail += '</div></td></tr>'
        return '<h1>Remote Senior Backend Engineer</h1><table><tbody>' + row + (detail if self.expanded else '') + '</tbody></table>' + \
               '<aside><a class="action-apply" data-job-id="123" href="https://evil.test/global">Apply now</a></aside>'

    def run_source(self, history=None):
        history = history if history is not None else {}
        return sources.run(apply, self.page, history, 0), history

    def test_complete_detail_loaded_before_apply_and_only_native_current_link_is_used(self):
        count, _ = self.run_source()
        self.assertEqual(count, 1)
        self.follow.assert_called_once()
        self.assertEqual(self.follow.call_args.args[1].get_attribute('href'), '/l/123')
        self.external.assert_called_once()
        self.global_apply.assert_not_called()

    def test_detail_instruction_missing_from_api_defers_before_any_apply_action(self):
        self.native += ' Include the phrase "green sky" in your cover letter.'
        count, history = self.run_source()
        self.assertEqual(count, 0)
        self.assertEqual(history['remoteok:123']['status'], 'manual_review')
        self.assertIn('green sky', history['remoteok:123']['description_instructions'][0])
        self.follow.assert_not_called()

    def test_feed_only_board_instruction_is_preserved_alongside_full_body(self):
        self.api_desc += ' Please mention the word BLUE in your application.'
        count, history = self.run_source()
        self.assertEqual(count, 0)
        self.assertIn('BLUE', history['remoteok:123']['description_instructions'][0])
        self.follow.assert_not_called()

    def test_explicit_requirement_in_full_body_is_checked_before_apply(self):
        self.native += ' Forbidden requirement.'
        apply.CFG['filters']['description_exclude'] = ['forbidden requirement']
        count, history = self.run_source()
        self.assertEqual(count, 0)
        self.assertEqual(history['remoteok:123']['status'], 'description_excluded')
        self.follow.assert_not_called()

    def test_hybrid_detail_cannot_be_overridden_by_remote_board(self):
        self.workplace = 'HYBRID'
        count, history = self.run_source()
        self.assertEqual(count, 0)
        self.assertEqual(history['remoteok:123']['status'], 'not_remote')
        self.follow.assert_not_called()

    def test_missing_or_truncated_description_never_falls_back_to_short_feed(self):
        for text in ('', 'Employer text...', 'Employer text... Please mention the word BLUE when applying.'):
            with self.subTest(text=text):
                self.native = text
                self.page.goto('about:blank')
                count, history = self.run_source()
                self.assertEqual(count, 0)
                self.assertEqual(history['remoteok:123']['reason'], 'description_unknown')
                self.follow.assert_not_called()

    def test_complete_verified_jsonld_can_supply_body_when_visible_text_is_truncated(self):
        self.native = 'Employer text...'
        self.structured = 'Full employer text with all requirements.'
        count, _ = self.run_source()
        self.assertEqual(count, 1)

    def test_missing_expanded_detail_does_not_use_other_page_text_or_links(self):
        self.expanded = False
        count, history = self.run_source()
        self.assertEqual(count, 0)
        self.assertIn(history['remoteok:123']['reason'], ('description_unknown', 'detail_identity_unknown'))
        self.follow.assert_not_called()

    def test_invalid_disabled_or_other_job_links_never_fall_back_to_global_apply(self):
        variants = [[], [('/l/999', '123', False)], [('https://evil.test/l/123', '123', False)],
                    [('/l/123', '123', True)], [('/l/999', '999', False)],
                    [('/l/123', '123', False), ('/l/999', '123', False)]]
        for links in variants:
            with self.subTest(links=links):
                self.links = links
                self.page.goto('about:blank')
                count, history = self.run_source()
                self.assertEqual(count, 0)
                self.assertEqual(history['remoteok:123']['status'], 'external_manual')
                self.follow.assert_not_called()
                self.global_apply.assert_not_called()

    def test_duplicate_buttons_for_the_same_native_destination_are_allowed(self):
        self.links *= 2
        self.assertEqual(self.run_source()[0], 1)
        self.follow.assert_called_once()

    def test_protected_history_prevents_detail_navigation(self):
        for status in ('applied', 'submission_pending', 'submission_unconfirmed', 'submission_rejected'):
            with self.subTest(status=status), patch.object(self.page, 'goto', wraps=self.page.goto) as goto:
                history = {'remoteok:123': {'status': status}}
                before = copy.deepcopy(history)
                self.assertEqual(self.run_source(history)[0], 0)
                self.assertEqual(history, before)
                goto.assert_not_called()
        self.follow.assert_not_called()

    def test_known_old_feed_job_is_rejected_before_description_fetch(self):
        self.epoch -= 8 * 86400
        with patch.object(self.page, 'goto', wraps=self.page.goto) as goto:
            count, history = self.run_source()
        self.assertEqual(count, 0)
        self.assertEqual(history['remoteok:123']['status'], 'posting_too_old')
        goto.assert_not_called()

    def test_untrusted_feed_url_never_navigates(self):
        self.feed_url = 'https://evil.test/remote-jobs/remote-senior-backend-engineer-example-123'
        with patch.object(self.page, 'goto', wraps=self.page.goto) as goto:
            count, history = self.run_source()
        self.assertEqual(count, 0)
        self.assertEqual(history['remoteok:123']['reason'], 'detail_identity_unknown')
        goto.assert_not_called()

    def test_description_change_after_review_prevents_apply(self):
        original = apply.check_job_eligibility
        def change(job, history):
            result = original(job, history)
            if result:
                self.page.locator('#native').evaluate("e=>e.textContent='Changed description. Include BLUE in your cover letter.'")
            return result
        with patch.object(apply, 'check_job_eligibility', side_effect=change):
            count, history = self.run_source()
        self.assertEqual(count, 0)
        self.assertEqual(history['remoteok:123']['status'], 'external_manual')
        self.follow.assert_not_called()

    def test_inspection_error_is_deferred_instead_of_aborting_source(self):
        with patch.object(self.page, 'evaluate', side_effect=RuntimeError('fixture inspection failure')):
            count, history = self.run_source()
        self.assertEqual(count, 0)
        self.assertEqual(history['remoteok:123']['reason'], 'description_unknown')
        self.assertEqual(apply.METRICS.snapshot()['errors'], 1)
        self.follow.assert_not_called()

    def test_blocked_detail_stops_source_instead_of_repeating_verification_per_job(self):
        feed = [{'legal': 'terms'}] + [
            {'id': jid, 'position': 'Senior Backend Engineer', 'company': 'Example',
             'url': self.URL[:-3] + jid, 'description': self.api_desc, 'epoch': self.epoch}
            for jid in ('123', '124')]
        with patch.object(sources, 'http', return_value=json.dumps(feed)), patch.object(sources, 'goto', return_value=False) as goto:
            count, history = self.run_source()
        self.assertEqual(count, 0)
        goto.assert_called_once()
        self.assertNotIn('remoteok:124', history)
        self.follow.assert_not_called()


if __name__ == '__main__':
    unittest.main()
