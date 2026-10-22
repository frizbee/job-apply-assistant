"""Follow-up #3: uncertain Submit attempts must consume the run/source budget."""
import datetime as dt
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from queue import Queue
from unittest.mock import MagicMock, patch

from test_answer_safety import SafetyTest, node
import apply
import sources


class SubmissionLimitTests(SafetyTest):
    def setUp(self):
        super().setUp()
        home = Path(self.enterContext(tempfile.TemporaryDirectory(prefix='submission-limits-')))
        self.enterContext(patch.object(apply, 'APPLIED_FILE', home / 'applied.json'))
        self.enterContext(patch.object(apply, 'LOG_FILE', home / 'applications.csv'))
        self.enterContext(patch.object(apply, 'MANUAL_FILE', home / 'manual.csv'))
        self.enterContext(patch.object(apply, 'DRY_RUN', False))
        self.enterContext(patch.object(apply, 'STOP', threading.Event()))
        self.enterContext(patch.object(apply, 'DEADLINE', time.time() + 3600))
        self.enterContext(patch.object(apply, 'record_manual'))
        self.enterContext(patch.object(apply, 'job_ok', return_value=None))
        self.enterContext(patch.object(apply, 'applied_elsewhere', return_value=False))
        self.enterContext(patch.object(apply, 'check_job_eligibility', return_value=True))
        apply.CFG['limits'].update(max_applications_per_run=1, per_source=1)
        self.state = {}
        self.page = node()
        self.button = node()

    def submit(self, job_id, outcome='submission_unconfirmed'):
        wait = MagicMock(return_value=outcome)
        if isinstance(outcome, BaseException):
            wait.side_effect = outcome
        return apply.submit_application(self.page, self.button, job_id, 'Engineer', job_id,
            'https://example.test/job', wait, self.state)

    def source(self, name, outcome='submission_unconfirmed', size=3):
        def generate(page):
            for i in range(size):
                job_id = f'{name}:{i}'
                job = {'id': job_id, 'title': 'Engineer', 'company': job_id, 'desc': '',
                       'url': 'https://example.test/job',
                       'metadata': {'posted_at': dt.datetime.now(dt.timezone.utc), 'workplace': 'remote'}}
                yield job, lambda applied, job_id=job_id: self.submit(job_id, outcome)
        return generate

    def test_uncertain_source_loop_cannot_send_three_with_limit_one(self):
        with patch.object(sources, 'SOURCES', {'indeed': self.source('indeed')}):
            count = sources.run(apply, self.page, self.state, 0)
        self.assertEqual(count, 0)  # Still no confirmed success.
        self.assertEqual(self.button.click.call_count, 1)
        self.assertEqual(len(self.state), 1)

    def test_shared_guard_limits_direct_calls_even_without_loop(self):
        self.assertEqual(self.submit('indeed:1'), 'submission_unconfirmed')
        self.assertEqual(self.submit('indeed:2'), 'skipped_run_limit')
        self.button.click.assert_called_once()
        self.assertNotIn('indeed:2', self.state)

    def test_source_limit_allows_next_source_but_global_budget_is_shared(self):
        apply.CFG['limits']['max_applications_per_run'] = 2
        self.assertEqual(self.submit('indeed:1'), 'submission_unconfirmed')
        self.assertEqual(self.submit('indeed:2'), 'skipped_source_limit')
        self.assertEqual(self.submit('wellfound:1'), 'submission_unconfirmed')
        self.assertEqual(self.submit('remoteok:1'), 'skipped_run_limit')
        self.assertEqual(self.button.click.call_count, 2)

    def test_uncertain_mixed_source_loop_moves_to_next_source(self):
        apply.CFG['limits']['max_applications_per_run'] = 2
        with patch.object(sources, 'SOURCES', {'indeed': self.source('indeed'), 'wellfound': self.source('wellfound')}):
            self.assertEqual(sources.run(apply, self.page, self.state, 0), 0)
        self.assertEqual(set(self.state), {'indeed:0', 'wellfound:0'})
        self.assertEqual(self.button.click.call_count, 2)

    def test_all_clicked_outcomes_consume_quota(self):
        for i, outcome in enumerate(['applied', 'applied_external', 'submission_rejected', 'submission_unconfirmed', apply.PWTimeout('lost confirmation')]):
            with self.subTest(outcome=outcome):
                apply.reset_submission_attempts()
                self.submit(f'wellfound:{i}', outcome)
                self.assertEqual(self.submit(f'other:{i}'), 'skipped_run_limit')
                self.assertEqual(apply.ATTEMPTS['total'], 1)

    def test_click_timeout_still_consumes_quota(self):
        self.button.click.side_effect = apply.PWTimeout('may have reached server')
        self.assertEqual(self.submit('indeed:1'), 'submission_unconfirmed')
        self.assertEqual(self.submit('indeed:2'), 'skipped_run_limit')
        self.button.click.assert_called_once()

    def test_stop_before_submit_does_not_reserve(self):
        apply.STOP.set()
        with self.assertRaises(apply.Stopped):
            self.submit('indeed:1')
        self.assertEqual(apply.ATTEMPTS['total'], 0)
        self.button.click.assert_not_called()

    def test_stop_after_pending_before_click_releases_reservation(self):
        original = apply.record
        def record(*args, **kwargs):
            original(*args, **kwargs)
            if args[4] == 'submission_pending':
                apply.STOP.set()
        with patch.object(apply, 'record', side_effect=record), self.assertRaises(apply.Stopped):
            self.submit('indeed:1')
        self.assertEqual(apply.ATTEMPTS['total'], 0)
        self.button.click.assert_not_called()
        self.assertEqual(apply.load_applied()['indeed:1']['status'], 'submission_unconfirmed')

    def test_stop_after_click_keeps_consumed_reservation(self):
        with self.assertRaises(apply.Stopped):
            self.submit('indeed:1', apply.Stopped())
        self.assertEqual(apply.ATTEMPTS['total'], 1)
        self.assertEqual(self.submit('indeed:2'), 'skipped_run_limit')

    def test_pending_persistence_failure_releases_budget_and_never_clicks(self):
        with patch.object(apply, 'write_applied', side_effect=OSError('disk full')), self.assertRaises(OSError):
            self.submit('indeed:1')
        self.assertEqual(apply.ATTEMPTS['total'], 0)
        self.button.click.assert_not_called()
        self.assertEqual(self.submit('indeed:2'), 'submission_unconfirmed')

    def test_final_persistence_failure_cannot_refund_a_click(self):
        original = apply.write_applied
        calls = 0
        def save(state):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError('final write failed')
            original(state)
        with patch.object(apply, 'write_applied', side_effect=save), self.assertRaises(OSError):
            self.submit('indeed:1', 'applied')
        self.assertEqual(apply.ATTEMPTS['total'], 1)
        self.assertEqual(self.submit('indeed:2'), 'skipped_run_limit')
        self.assertEqual(apply.load_applied()['indeed:1']['status'], 'submission_pending')

    def test_prior_history_does_not_spend_new_budget(self):
        for status in ['applied', 'submission_pending', 'submission_unconfirmed', 'manual_review']:
            self.state['indeed:old'] = {'status': status}
            self.submit('indeed:old')
        self.assertEqual(apply.ATTEMPTS['total'], 0)
        self.button.click.assert_not_called()

    def test_zero_total_or_source_limit_blocks_submit_and_preview(self):
        for field in ['max_applications_per_run', 'per_source']:
            with self.subTest(field=field):
                apply.CFG['limits'].update(max_applications_per_run=1, per_source=1)
                apply.CFG['limits'][field] = 0
                self.assertTrue(self.submit('indeed:new').startswith('skipped_'))
                with patch.object(apply, 'DRY_RUN', True):
                    self.assertTrue(self.submit('indeed:new').startswith('skipped_'))
        self.button.click.assert_not_called()
        self.assertEqual(self.state, {})

    def test_dry_run_is_limited_without_clicks_or_history(self):
        with patch.object(apply, 'DRY_RUN', True):
            self.assertEqual(self.submit('indeed:1'), 'dry_run')
            self.assertEqual(self.submit('indeed:2'), 'skipped_run_limit')
        self.button.click.assert_not_called()
        self.assertEqual(self.state, {})
        self.assertFalse(apply.APPLIED_FILE.exists())

    def test_external_attempt_is_charged_to_originating_board(self):
        apply.CFG['limits']['max_applications_per_run'] = 2
        with apply.submission_source('remoteok'):
            self.assertEqual(self.submit('arbitrary-external-id'), 'submission_unconfirmed')
            self.assertEqual(self.submit('another-external-id'), 'skipped_source_limit')
        self.assertEqual(self.submit('wellfound:1'), 'submission_unconfirmed')
        self.assertEqual(apply.ATTEMPTS['by_source'], {'remoteok': 1, 'wellfound': 1})

    def test_wwr_alias_cannot_create_a_separate_budget(self):
        apply.CFG['limits']['max_applications_per_run'] = 2
        self.submit('wwr:1')
        self.assertEqual(self.submit('weworkremotely:2'), 'skipped_source_limit')

    def test_new_limit_statuses_are_retryable(self):
        self.assertTrue(apply.can_retry('skipped_run_limit'))
        self.assertTrue(apply.can_retry('skipped_source_limit'))

    def test_run_bot_resets_attempts_after_success_and_failure(self):
        apply.CFG['sources']['linkedin'] = False
        ctx = node()
        ctx.pages = [self.page]
        seen = []
        def work(*args):
            seen.append(apply.ATTEMPTS['total'])
            apply.reserve_submission_attempt('indeed')
            if len(seen) == 2:
                raise RuntimeError('source failed after reserving')
            return 0
        with patch.object(apply, 'load_applied', return_value={}), patch.object(apply, 'sync_playwright'), \
                patch.object(apply, 'launch', return_value=ctx), patch.object(apply, 'LAST_RUN_FILE', node()), \
                patch.object(sources, 'run', side_effect=work):
            apply.run_bot()
            with self.assertRaises(RuntimeError):
                apply.run_bot()
            apply.run_bot()
        self.assertEqual(seen, [0, 0, 0])
        self.assertEqual(apply.ATTEMPTS['total'], 1)

    def test_linkedin_loop_stops_after_uncertain_submit(self):
        def process(page, applied, job_id):
            return self.submit(job_id) == 'applied'
        with patch.object(apply, 'linkedin_login', return_value=True), patch.object(apply, 'job_lists', return_value=[['1', '2', '3']]), \
                patch.object(apply, 'process_job', side_effect=process):
            self.assertEqual(apply.run_linkedin(self.page, self.state, 0), 0)
        self.button.click.assert_called_once()

    def test_concurrent_guard_calls_can_reserve_only_one_slot(self):
        barrier, results = threading.Barrier(3), Queue()
        def request(job_id):
            barrier.wait(timeout=5)
            try:
                results.put(self.submit(job_id))
            except BaseException as error:
                results.put(error)
        threads = [threading.Thread(target=request, args=(f'indeed:{i}',), daemon=True) for i in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait(timeout=5)
        for thread in threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())
        self.assertCountEqual([results.get_nowait(), results.get_nowait()], ['submission_unconfirmed', 'skipped_run_limit'])
        self.button.click.assert_called_once()

    def test_budget_is_reserved_before_pending_record_and_click(self):
        original = apply.record
        def record(*args, **kwargs):
            self.assertEqual(apply.ATTEMPTS['total'], 1)
            original(*args, **kwargs)
        with patch.object(apply, 'record', side_effect=record):
            self.submit('indeed:1')

    def test_failed_browser_start_does_not_poison_next_runs_budget(self):
        apply.ATTEMPTS.update(total=1, by_source={'indeed': 1})
        with patch.object(apply, 'load_applied', return_value={}), patch.object(apply, 'sync_playwright'), \
                patch.object(apply, 'launch', side_effect=RuntimeError('no browser')), self.assertRaises(RuntimeError):
            apply.run_bot()
        self.assertEqual(apply.ATTEMPTS, {'total': 0, 'by_source': {}})

    def test_zero_source_budget_does_not_even_open_the_source(self):
        apply.CFG['limits']['per_source'] = 0
        source = MagicMock()
        login = MagicMock()
        with patch.object(sources, 'SOURCES', {'indeed': source}), patch.object(apply, 'linkedin_login', login):
            self.assertEqual(sources.run(apply, self.page, self.state, 0), 0)
            self.assertEqual(apply.run_linkedin(self.page, self.state, 0), 0)
        source.assert_not_called()
        login.assert_not_called()

    def test_source_context_is_restored_after_stop(self):
        with self.assertRaises(apply.Stopped):
            with apply.submission_source('indeed'):
                self.assertEqual(apply.source_for_job('123'), 'indeed')
                raise apply.Stopped()
        self.assertEqual(apply.source_for_job('123'), 'linkedin')

    def entrypoint(self, kind, number):
        page, form, send = node(), node(), node()
        page.url = {'linkedin': 'https://www.linkedin.com/jobs/view/123/',
                    'ats': 'https://jobs.lever.co/example/job/apply'}.get(kind, f'https://{kind}.com/jobs/123')
        page.context.pages = [page]
        page.goto.side_effect = lambda url, **kwargs: setattr(page, 'url', url)
        page.evaluate.return_value = True
        empty = node()
        empty.count.return_value = 0
        form.locator.side_effect = lambda selector: empty if selector == 'input#follow-company-checkbox:checked' else form
        form.inner_text.return_value = 'Submit your application'
        form.evaluate.side_effect = lambda script, *args: True if script == apply.NON_SUBMITTING_CONTROL_JS else []
        form.get_by_role.return_value = send
        page.locator.return_value = form
        opener = node()
        opener.evaluate.return_value = True
        page.get_by_role.return_value = send if kind == 'indeed' else opener
        send.inner_text.return_value = 'Submit your application'
        job_id = f'{kind}:{number}' if kind != 'linkedin' else str(number)
        with patch.object(sources, 'core', apply), patch.object(sources, 'goto', return_value=True), \
                patch.object(sources, 'wellfound_login', return_value=True), patch.object(apply, 'wait_human', return_value=True), \
                patch.object(apply, 'fill_step', return_value=[]), patch.object(apply, 'require_manual_review', return_value=False), \
                patch.object(apply, 'wait_linkedin_submission', return_value='submission_unconfirmed'), \
                patch.object(apply, 'wait_application_submission', return_value='submission_unconfirmed'), \
                patch.object(apply, 'EXTERNAL', True), patch.object(apply, 'EXT_STATE', {'count': 0, 'last': 0.0}):
            if kind == 'linkedin':
                result = apply.apply_to_job(page, job_id, 'Engineer', job_id, self.state)
                button = form
            elif kind == 'ats':
                result = apply.apply_form(page, job_id, 'Engineer', job_id, page.url, self.state)
                button = form
            else:
                job = {'id': job_id, 'title': 'Engineer', 'company': job_id, 'url': page.url}
                result = getattr(sources, kind + '_apply')(page, job, self.state)
                button = send
        return result, button

    def test_every_submitter_stops_after_one_uncertain_final_click(self):
        for kind in ['linkedin', 'indeed', 'wellfound', 'ats']:
            with self.subTest(kind=kind):
                apply.reset_submission_attempts()
                self.state.clear()
                result, first = self.entrypoint(kind, 1)
                self.assertEqual(result, 'submission_unconfirmed')
                result, second = self.entrypoint(kind, 2)
                self.assertEqual(result, 'skipped_run_limit')
                first.click.assert_called_once()
                second.click.assert_not_called()

    def test_every_submitter_limits_dry_run_without_submit_or_history(self):
        for kind in ['linkedin', 'indeed', 'wellfound', 'ats']:
            with self.subTest(kind=kind), patch.object(apply, 'DRY_RUN', True):
                apply.reset_submission_attempts()
                self.assertEqual(self.entrypoint(kind, 1)[0], 'dry_run')
                self.assertEqual(self.entrypoint(kind, 2)[0], 'skipped_run_limit')
                self.assertEqual(self.state, {})
                self.assertFalse(apply.APPLIED_FILE.exists())


@unittest.skipUnless(os.getenv('AUTOAPPLY_BROWSER_TESTS') == '1', 'optional local Chrome submission-budget tests')
class BrowserSubmissionLimitTests(SafetyTest):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.pw = sync_playwright().start()
        cls.addClassCleanup(cls.pw.stop)
        cls.browser = cls.pw.chromium.launch(channel='chrome', headless=True)
        cls.addClassCleanup(cls.browser.close)

    def setUp(self):
        super().setUp()
        home = Path(self.enterContext(tempfile.TemporaryDirectory(prefix='browser-submission-limits-')))
        self.enterContext(patch.object(apply, 'APPLIED_FILE', home / 'applied.json'))
        self.enterContext(patch.object(apply, 'LOG_FILE', home / 'applications.csv'))
        self.enterContext(patch.object(apply, 'record_manual'))
        self.enterContext(patch.object(apply, 'DRY_RUN', False))
        self.enterContext(patch.object(apply, 'STOP', threading.Event()))
        apply.CFG['limits'].update(max_applications_per_run=1, per_source=1)
        self.context = self.browser.new_context()
        self.addCleanup(self.context.close)
        self.context.route('**/*', lambda route: route.abort())
        self.page = self.context.new_page()
        self.page.set_default_timeout(1000)
        self.page.set_content('''<script>window.sent=0</script><button type="button">Easy Apply</button>
            <div role="dialog"><form onsubmit="event.preventDefault();window.sent++">
            <footer><button type="submit">Submit application</button></footer></form></div>''')
        self.state = {}

    def submit(self, job_id):
        return apply.submit_application(self.page, self.page.locator('button[type=submit]'), job_id,
            'Engineer', job_id, 'https://example.test/job', lambda: 'submission_unconfirmed', self.state)

    def test_uncertain_submit_only_fires_once_with_limit_one(self):
        self.assertEqual(self.submit('indeed:1'), 'submission_unconfirmed')
        self.assertEqual(self.submit('indeed:2'), 'skipped_run_limit')
        self.assertEqual(self.page.evaluate('window.sent'), 1)
        self.assertEqual(set(apply.load_applied()), {'indeed:1'})

    def test_mixed_sources_share_total_and_respect_per_source_budget(self):
        apply.CFG['limits']['max_applications_per_run'] = 2
        self.assertEqual(self.submit('indeed:1'), 'submission_unconfirmed')
        self.assertEqual(self.submit('indeed:2'), 'skipped_source_limit')
        self.assertEqual(self.submit('wellfound:1'), 'submission_unconfirmed')
        self.assertEqual(self.submit('remoteok:1'), 'skipped_run_limit')
        self.assertEqual(self.page.evaluate('window.sent'), 2)

    def test_dry_run_entrypoint_uses_preview_budget_without_submit(self):
        with patch.object(apply, 'DRY_RUN', True):
            self.assertEqual(apply.apply_to_job(self.page, '1', 'Engineer', 'Example'), 'dry_run')
            self.assertEqual(apply.apply_to_job(self.page, '2', 'Engineer', 'Other'), 'skipped_run_limit')
        self.assertEqual(self.page.evaluate('window.sent'), 0)
        self.assertFalse(apply.APPLIED_FILE.exists())
