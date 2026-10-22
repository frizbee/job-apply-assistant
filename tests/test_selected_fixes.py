"""User title exclusions, safe Wellfound forms, and truthful workflow counters."""
import os
import unittest
from pathlib import Path
import tempfile
from unittest.mock import patch

import apply
import cv_profile
import sources
from test_answer_safety import SafetyTest, node


class TitleExclusionTests(SafetyTest):
    def setUp(self):
        super().setUp()
        prof = cv_profile.empty_profile()
        prof['job_titles'] = ['Lead Software Engineer', 'Senior Backend Engineer']
        apply.apply_cv_profile(prof)

    def test_no_implicit_marketing_exclusion_or_change_to_positive_matching(self):
        self.assertIsNone(apply.job_ok('Product Marketing Lead', 'Example', ''))

    def test_user_term_matches_title_only_with_literal_word_boundaries(self):
        apply.SETTINGS['excluded_title_words'] = ['Marketing']
        self.assertEqual(apply.job_ok('Product MARKETING Lead', 'Example', ''), 'title_keyword_excluded')
        self.assertIsNone(apply.job_ok('Lead Software Engineer', 'Example', 'Working with the Marketing team.'))
        self.assertIsNone(apply.job_ok('MarketingTools Software Engineer', 'Example', ''))
        apply.SETTINGS['excluded_title_words'] = ['Marketing.*']
        self.assertIsNone(apply.job_ok('Product Marketing Lead', 'Example', ''))
        apply.SETTINGS['excluded_title_words'] = []
        self.assertIsNone(apply.job_ok('Product Marketing Lead', 'Example', ''))

    def test_rejection_evidence_and_revision_preserve_previous_decision(self):
        import json
        import job_rechecks
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(apply, 'APPLIED_FILE', Path(folder) / 'applied.json'), \
             patch.object(apply, 'LOG_FILE', Path(folder) / 'applications.csv'):
            history = {'job': {'status': 'title_not_relevant', 'title': 'Previous title'}}
            revision = job_rechecks.revision(apply.CFG, apply.SETTINGS)
            apply.SETTINGS['excluded_title_words'] = ['Marketing']
            self.assertNotEqual(revision, job_rechecks.revision(apply.CFG, apply.SETTINGS))
            apply.record_relevance_decision(history, 'job', 'Marketing Lead', 'Example', 'title_keyword_excluded', 'https://example.test')
            saved = json.loads(apply.APPLIED_FILE.read_text(encoding='utf-8'))['job']
            self.assertEqual(saved['title_diagnostic']['term'], 'Marketing')
            self.assertEqual(saved['decisions'][0]['title'], 'Previous title')


class MetricsTests(SafetyTest):
    def test_sets_deduplicate_retries_and_do_not_count_blocked_as_filled(self):
        m = apply.METRICS
        for _ in range(2):
            m.mark('seen', 'job')
            m.mark('evaluated', 'job')
            m.outcome('job', 'blocked')
        self.assertEqual(m.snapshot()['seen'], 1)
        self.assertEqual(m.snapshot()['manual'], 1)
        self.assertEqual(m.snapshot()['forms_changed'], 0)
        self.assertEqual(m.snapshot()['previews'], 0)
        self.assertIn('seen: 1', m.summary())

    def test_application_context_restored_and_metrics_reset(self):
        m = apply.METRICS
        with m.application('job'):
            m.mark('forms_opened')
            with m.application('nested'):
                m.mark('forms_changed')
            self.assertEqual(m.current, 'job')
        self.assertIsNone(m.current)
        m.reset()
        self.assertFalse(any(m.snapshot().values()))

    def test_source_rejections_count_as_evaluated_not_previews(self):
        apply.CFG['sources'] = {'wellfound': True}
        apply.DEADLINE = float('inf')
        job = {'id': 'wellfound:job', 'title': 'Marketing Lead', 'company': 'Example', 'desc': '', 'url': 'https://example.test'}
        send = node()
        with patch.object(sources, 'SOURCES', {'wellfound': lambda p: iter([(job, send)])}), \
             patch.object(apply, 'job_ok', return_value='title_keyword_excluded'), \
             patch.object(apply, 'record'):
            self.assertEqual(sources.run(apply, node(), {}, 0), 0)
        send.assert_not_called()
        counts = apply.METRICS.snapshot()
        self.assertEqual((counts['seen'], counts['evaluated'], counts['rejected'], counts['previews']), (1, 1, 1, 0))

    def test_real_run_summary_counts_rejections_without_calling_them_tested(self):
        apply.CFG['sources'] = {'wellfound': True, 'linkedin': False}
        jobs = [{'id': f'wellfound:{i}', 'title': 'Marketing Lead', 'company': 'Example', 'desc': '', 'url': 'https://example.test'} for i in range(2)]
        ctx = node()
        ctx.pages = [node()]
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(apply, 'LAST_RUN_FILE', Path(folder) / 'last_run.txt'), \
             patch.object(apply, 'load_applied', return_value={}), \
             patch.object(apply, 'sync_playwright'), patch.object(apply, 'launch', return_value=ctx), \
             patch.object(sources, 'SOURCES', {'wellfound': lambda p: iter((j, node()) for j in jobs)}), \
             patch.object(apply, 'job_ok', return_value='title_keyword_excluded'), patch.object(apply, 'record'):
            apply.run_bot()
        summary = next(c.args[0] for c in apply.log.call_args_list if c.args[0].startswith('Finished.'))
        self.assertIn('seen: 2', summary)
        self.assertIn('evaluated: 2', summary)
        self.assertIn('rejected: 2', summary)
        self.assertIn('previews: 0', summary)
        self.assertNotIn('Tested', summary)


@unittest.skipUnless(os.getenv('AUTOAPPLY_BROWSER_TESTS') == '1', 'optional offline Chrome fixtures')
class WellfoundFormTests(SafetyTest):
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
        self.html = ''
        self.ctx.route('**/*', lambda route: route.fulfill(content_type='text/html', body=self.html))
        self.enterContext(patch.object(sources, 'core', apply))
        self.enterContext(patch.object(sources, 'wellfound_login', return_value=True))
        self.enterContext(patch.object(sources, 'goto', side_effect=lambda p, u, n: (p.goto(u), True)[1]))
        self.enterContext(patch.object(apply, 'record_manual'))
        self.enterContext(patch.object(apply, 'DRY_RUN', True))
        apply.CFG['skill_years'] = {'ruby': 14}
        self.job = {'id': 'wellfound:123', 'url': 'https://wellfound.com/jobs/123-engineer', 'title': 'Engineer', 'company': 'Example'}

    def fixture(self, button_type='button', form_count=1):
        self.html = f'''<meta charset="utf-8"><script>window.sent=0;window.wrong=0</script>
        <aside><button type="button" onclick="window.wrong++">Apply</button><a href="/jobs/999-other">Other job</a></aside>
        <section><h1>Engineer</h1><button type="{button_type}" onclick="document.getElementById('modal').style.display='block'">Apply Now</button></section>
        <div id="modal" role="dialog" style="display:none"><form onsubmit="event.preventDefault();window.sent++">
        <label for="ruby">Years of Ruby experience?</label><input id="ruby" type="number" required>
        <button type="submit">Submit application</button></form></div>'''
        if form_count > 1:
            self.html += '<div role="dialog">Other form</div>'

    def test_correct_opener_and_form_filled_without_submit_or_recommendation_click(self):
        self.fixture()
        with apply.METRICS.application(self.job['id']):
            self.assertEqual(sources.wellfound_apply(self.page, self.job, {}), 'dry_run')
        self.assertEqual(self.page.locator('#ruby').input_value(), '14')
        self.assertEqual(self.page.evaluate('window.sent'), 0)
        self.assertEqual(self.page.evaluate('window.wrong'), 0)
        counts = apply.METRICS.snapshot()
        self.assertEqual((counts['forms_opened'], counts['forms_changed']), (1, 1))

    def test_native_submit_opener_stays_blocked(self):
        self.fixture(button_type='submit')
        self.assertEqual(sources.wellfound_apply(self.page, self.job, {}), 'manual_review')
        self.assertEqual(self.page.evaluate('window.sent'), 0)
        self.assertEqual(self.page.evaluate('window.wrong'), 0)

    def test_ambiguous_forms_do_not_fill_or_preview(self):
        self.fixture(form_count=2)
        self.assertEqual(sources.wellfound_apply(self.page, self.job, {}), 'manual_review')
        self.assertEqual(self.page.locator('#ruby').input_value(), '')
        self.assertEqual(apply.ATTEMPTS['total'], 0)

    def test_generic_search_redirect_yields_no_jobs_and_records_failure(self):
        apply.CFG['search']['keywords'] = ['Senior Backend Engineer']
        self.html = '<h1>Remote jobs</h1><a href="/jobs/999-irrelevant">Marketing Lead</a>'
        self.ctx.unroute('**/*')
        def route(request):
            if '/role/r/' in request.request.url:
                request.fulfill(status=302, headers={'location': 'https://wellfound.com/remote'})
            else:
                request.fulfill(content_type='text/html', body=self.html)
        self.ctx.route('**/*', route)
        self.assertEqual(list(sources._wellfound_public(self.page)), [])
        self.assertEqual(apply.METRICS.snapshot()['searches_failed'], 1)
        self.assertEqual(apply.METRICS.snapshot()['seen'], 0)
        self.assertTrue(any('generic results will not be processed' in str(c) for c in apply.log.call_args_list))

    def test_search_requires_exact_active_role_not_just_matching_url(self):
        url = 'https://wellfound.com/role/r/backend-engineer'
        self.html = '<div class="select__control"><span class="select__single-value">Software Engineer</span><input id="react-select-1-input"></div>'
        self.page.goto(url)
        self.assertFalse(sources.wellfound_search_verified(self.page, url, 'Backend Engineer'))
        self.page.locator('.select__single-value').evaluate("e=>e.textContent='Backend Engineer'")
        self.assertTrue(sources.wellfound_search_verified(self.page, url, 'Backend Engineer'))

    def test_preview_is_not_counted_as_a_submit_attempt(self):
        self.fixture()
        with apply.METRICS.application(self.job['id']):
            self.assertEqual(sources.wellfound_apply(self.page, self.job, {}), 'dry_run')
        self.assertEqual(apply.METRICS.snapshot()['previews'], 1)
        self.assertIn('submit_attempts: 0', apply.METRICS.summary(submit_attempts=0))

    def test_ambiguous_final_buttons_do_not_create_a_preview(self):
        self.fixture()
        self.html = self.html.replace('</form>', '<button type="submit">Send application</button></form>')
        self.assertEqual(sources.wellfound_apply(self.page, self.job, {}), 'manual_review')
        self.assertEqual(self.page.evaluate('window.sent'), 0)
        self.assertEqual(apply.ATTEMPTS['total'], 0)
