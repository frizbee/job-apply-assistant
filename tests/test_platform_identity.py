"""Canonical ATS identity, durable guards and non-consuming duplicate handling."""
import csv
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

import apply
import application_identity as identity
from test_answer_safety import SafetyTest, node

ASHBY = 'https://jobs.ashbyhq.com/kraken.com/4753ccbf-68dc-4668-904f-a3156041fdc8'


class PlatformIdentityTests(SafetyTest):
    def setUp(self):
        super().setUp()
        home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(apply, 'APPLIED_FILE', home / 'history.json'))
        self.enterContext(patch.object(apply, 'LOG_FILE', home / 'applications.csv'))
        self.enterContext(patch.object(apply, 'MANUAL_FILE', home / 'manual.csv'))
        self.enterContext(patch.object(apply, 'EXT_STATE', {'count': 0, 'last': 0.0}))
        self.enterContext(patch.object(apply, 'EXTERNAL', True))
        self.enterContext(patch.object(apply, 'DRY_RUN', True))
        self.enterContext(patch.object(apply, '_APPLICATION_LEDGER', identity.ApplicationLedger(apply.EXT_STATE), create=True))
        self.page = node()
        self.page.url = ASHBY + '/application'
        self.history = {}

    def key(self, url):
        return identity.application_key(url, apply)

    def test_tracking_and_application_suffix_do_not_change_the_job(self):
        self.assertEqual(self.key(ASHBY), self.key(ASHBY + '/application?utm_source=LinkedIn&src=x#top'))
        self.assertNotEqual(self.key(ASHBY), self.key(ASHBY.replace('4753ccbf', '4753ccba')))
        self.assertEqual(self.key('https://boards.greenhouse.io/alpaca/jobs/6207266004?gh_src=x'),
                         self.key('https://job-boards.greenhouse.io/alpaca/jobs/6207266004'))
        self.assertEqual(self.key('https://boards.greenhouse.io/embed/job_app?for=alpaca&token=6207266004'),
                         self.key('https://job-boards.greenhouse.io/alpaca/jobs/6207266004'))
        self.assertNotEqual(self.key('https://boards.eu.greenhouse.io/alpaca/jobs/6207266004'),
                            self.key('https://boards.greenhouse.io/alpaca/jobs/6207266004'))

    def test_unsafe_urls_and_unknown_paths_have_no_identity(self):
        for url in [ASHBY.replace('jobs.ashbyhq.com', 'evil.example'), ASHBY.replace('https:', 'http:'),
                    ASHBY.replace('jobs.ashbyhq.com', 'jobs.ashbyhq.com.evil.example'),
                    ASHBY.replace('jobs.ashbyhq.com', 'user@jobs.ashbyhq.com'), 'https://jobs.ashbyhq.com/company/not-a-job']:
            self.assertIsNone(self.key(url))

    def test_same_application_from_two_board_ids_is_filled_once(self):
        with patch.object(apply, 'apply_trusted_form', return_value='manual_review') as fill:
            self.assertEqual(apply.apply_form(self.page, '1', 'Engineer', 'Kraken', ASHBY, self.history), 'manual_review')
            self.assertEqual(apply.apply_form(self.page, '2', 'Different title', 'Kraken', ASHBY + '/application?utm_source=another', self.history), 'external_duplicate')
            self.assertEqual(apply.apply_form(self.page, '1', 'Engineer', 'Kraken', ASHBY, self.history), 'manual_review')
        fill.assert_called_once()
        self.assertEqual(self.page.goto.call_count, 1)
        self.assertEqual(apply.EXT_STATE['count'], 1)

    def test_binding_survives_restart_without_migrating_old_records(self):
        with patch.object(apply, 'apply_trusted_form', return_value='manual_review'):
            apply.apply_form(self.page, '1', 'Engineer', 'Kraken', ASHBY, self.history)
        apply.record(self.history, '1', 'Engineer', 'Kraken', 'manual_review')
        saved = apply.load_applied()
        self.assertEqual(saved['1']['application_key'], self.key(ASHBY))
        identity.reset(apply)
        with patch.object(apply, 'apply_trusted_form') as fill:
            self.assertEqual(apply.apply_form(self.page, '2', 'Engineer', 'Kraken', ASHBY, saved), 'external_duplicate')
        fill.assert_not_called()

    def test_old_csv_urls_are_read_without_rewriting_history(self):
        self.history = {'old': {'status': 'manual_review', 'url': 'https://www.linkedin.com/jobs/view/1/'}}
        with apply.MANUAL_FILE.open('w', encoding='utf-8', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=['job_id', 'apply_url'])
            writer.writeheader(); writer.writerow({'job_id': 'old', 'apply_url': ASHBY + '/application?src=old'})
        with patch.object(apply, 'write_applied') as write, patch.object(apply, 'apply_trusted_form') as fill:
            self.assertEqual(apply.apply_form(self.page, 'new', 'Engineer', 'Kraken', ASHBY, self.history), 'external_duplicate')
        write.assert_not_called(); fill.assert_not_called()
        self.assertNotIn('application_key', self.history['old'])

    def test_limit_skip_does_not_claim_the_application_as_processed(self):
        apply.EXT_STATE['count'] = apply.CFG['external'].get('max_per_run', 5)
        self.assertEqual(apply.apply_form(self.page, '1', 'Engineer', 'Kraken', ASHBY, self.history), 'skipped_external_limit')
        self.assertEqual(identity.ledger(apply).attempts, {})
        apply.EXT_STATE['count'] = 0
        with patch.object(apply, 'apply_trusted_form', return_value='dry_run') as fill:
            self.assertEqual(apply.apply_form(self.page, '2', 'Engineer', 'Kraken', ASHBY, self.history), 'dry_run')
        fill.assert_called_once()

    def test_pending_identity_is_persisted_before_the_final_click(self):
        with patch.object(apply, 'DRY_RUN', False):
            button = node()
            def click():
                pending = json.loads(apply.APPLIED_FILE.read_text(encoding='utf-8'))['1']
                self.assertEqual(pending['status'], 'submission_pending')
                self.assertEqual(pending['application_key'], self.key(ASHBY))
            button.click.side_effect = click
            def fill(page, job_id, title, company, url, provider, history):
                return apply.submit_application(page, button, job_id, title, company, url, lambda: 'applied_external', history)
            with patch.object(apply, 'apply_trusted_form', side_effect=fill):
                self.assertEqual(apply.apply_form(self.page, '1', 'Engineer', 'Kraken', ASHBY, self.history), 'applied_external')
            button.click.assert_called_once()
            identity.reset(apply)
            duplicate = node()
            self.assertEqual(apply.submit_application(self.page, duplicate, '2', 'Engineer', 'Kraken', ASHBY,
                                                     lambda: 'applied_external', self.history), 'external_duplicate')
            duplicate.click.assert_not_called()

    def test_manual_reopen_cannot_bypass_another_boards_uncertain_attempt(self):
        self.history = {'1': {'status': 'manual_review', 'application_key': self.key(ASHBY)},
                        'other': {'status': 'submission_unconfirmed', 'application_key': self.key(ASHBY)}}
        token = apply.deferred_review.CURRENT.set(apply.deferred_review.Session('1', True))
        self.addCleanup(apply.deferred_review.CURRENT.reset, token)
        with patch.object(apply, 'apply_trusted_form') as fill:
            self.assertEqual(apply.apply_form(self.page, '1', 'Engineer', 'Kraken', ASHBY, self.history), 'external_duplicate')
        fill.assert_not_called()

    def test_manual_user_can_select_one_of_legacy_queued_aliases(self):
        self.history = {key: {'status': 'manual_review', 'application_key': self.key(ASHBY)} for key in ['1', '2']}
        token = apply.deferred_review.CURRENT.set(apply.deferred_review.Session('2', True))
        self.addCleanup(apply.deferred_review.CURRENT.reset, token)
        with patch.object(apply, 'apply_trusted_form', return_value='dry_run') as fill:
            self.assertEqual(apply.apply_form(self.page, '2', 'Engineer', 'Kraken', ASHBY, self.history), 'dry_run')
        fill.assert_called_once()

    def test_new_pending_history_takes_priority_over_an_old_cached_manual_result(self):
        with patch.object(apply, 'apply_trusted_form', return_value='manual_review'):
            apply.apply_form(self.page, '1', 'Engineer', 'Kraken', ASHBY, self.history)
        self.history['other'] = {'status': 'submission_pending', 'application_key': self.key(ASHBY)}
        with patch.object(apply, 'apply_trusted_form') as fill:
            self.assertEqual(apply.apply_form(self.page, '1', 'Engineer', 'Kraken', ASHBY, self.history), 'external_duplicate')
        fill.assert_not_called()

    def test_queue_groups_legacy_ats_aliases_without_changing_records(self):
        history = {'one': {'status': 'manual_review', 'url': ASHBY},
                   'two': {'status': 'manual_review', 'url': ASHBY + '/application?src=other'}}
        rows = apply.deferred_review.queue(history, apply.MANUAL_FILE, apply)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['related_job_ids'], ['one', 'two'])
        self.assertNotIn('application_key', history['one'])
        history['two']['status'] = 'submission_pending'
        self.assertEqual(apply.deferred_review.queue(history, apply.MANUAL_FILE, apply), [])

    def test_queue_does_not_merge_titles_or_unsupported_urls(self):
        history = {key: {'status': 'manual_review', 'title': 'Engineer', 'company': 'Same', 'url': url}
                   for key, url in [('one', 'https://unsupported.example/one'), ('two', 'https://unsupported.example/two')]}
        self.assertEqual(len(apply.deferred_review.queue(history, apply.MANUAL_FILE, apply)), 2)

    def test_known_manual_form_takes_priority_over_a_limited_alias(self):
        history = {'limit': {'status': 'skipped_external_limit', 'url': ASHBY},
                   'manual': {'status': 'manual_review', 'url': ASHBY + '/application'}}
        rows = apply.deferred_review.queue(history, apply.MANUAL_FILE, apply)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['job_id'], 'manual')
        self.assertEqual(rows[0]['category'], 'manual')

    def test_redirect_to_a_different_job_on_the_same_ats_is_not_filled(self):
        self.page.goto.side_effect = lambda *a, **kw: setattr(self.page, 'url', ASHBY.replace('4753ccbf', '4753ccba'))
        with patch.object(apply, 'apply_trusted_form') as fill:
            self.assertEqual(apply.apply_form(self.page, '1', 'Engineer', 'Kraken', ASHBY, self.history), 'external_manual')
        fill.assert_not_called()

    def test_duplicate_outcomes_are_counted_separately_from_manual_input(self):
        apply.METRICS.outcome('alias', 'external_duplicate')
        self.assertEqual(apply.METRICS.snapshot()['duplicates_skipped'], 1)
        self.assertEqual(apply.METRICS.snapshot()['manual'], 0)
