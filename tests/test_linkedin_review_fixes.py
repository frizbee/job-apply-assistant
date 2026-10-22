"""Real-layout regressions, Rails suitability and bounded decision rechecks."""
import datetime as dt
import json
import os
import re
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_answer_safety import SafetyTest
import apply
import job_metadata
import job_relevance
import job_rechecks

TARGET_ROLES = ['Senior Ruby on Rails Developer', 'Senior Ruby on Rails Engineer', 'Senior Ruby Developer',
                'Lead Ruby on Rails Developer', 'Ruby on Rails Technical Lead', 'Senior Backend Engineer',
                'Senior Backend Developer', 'Lead Software Engineer', 'Senior Full Stack Developer']


class LocalizedDateTests(unittest.TestCase):
    def test_localized_rounded_dates_are_conservative(self):
        now = dt.datetime(2026, 10, 22, tzinfo=dt.timezone.utc)
        for text in ['Reposted 2 days ago', 'Publicado hace 2 días', 'Republicado há 2 dias', '2 gün önce', '2 дня назад']:
            self.assertEqual(job_metadata.posting_time(text, now), now - dt.timedelta(days=3), text)

    def test_unknown_and_unbounded_labels_are_not_fresh(self):
        for text in ['', 'Recent', 'Hace 30+ días', 'há 30+ dias', 'Posted recently', 'Date posted filter: Past week']:
            self.assertIsNone(job_metadata.posting_time(text))


class UserTechnologyTests(unittest.TestCase):
    def test_empty_lists_never_infer_technology_or_specialty_exclusions(self):
        for tech in ['Ruby', 'Python', 'Django', 'Java', 'Spring', 'Go', 'C#', '.NET', 'Rust', 'Node.js', 'iOS', 'Kubernetes', 'ArbitraryStack']:
            self.assertIsNone(job_relevance.reason(f'Senior {tech} Engineer', f'{tech} is required.'), tech)

    def test_targets_come_only_from_the_user(self):
        for tech in ['Ruby', 'Python', 'Django', 'ArbitraryStack']:
            settings = {'target_technologies': [tech]}
            self.assertIsNone(job_relevance.reason(f'{tech} Engineer', '', **settings))
            self.assertIsNone(job_relevance.reason('Senior Backend Engineer', f'Build with {tech}.', **settings))
            self.assertEqual(job_relevance.reason('Senior Backend Engineer', '', **settings), 'stack_unknown')

    def test_only_user_excluded_title_or_required_stack_is_rejected(self):
        for tech in ['Python', 'Java', 'ArbitraryStack', 'C++', 'C#', '.NET', 'Node.js']:
            settings = {'excluded_technologies': [tech]}
            for title, desc in [(f'{tech} Engineer', ''), ('Engineer', f'{tech} is required.'),
                                ('Engineer', f'Technical Requirements:\n{tech}')]:
                self.assertEqual(job_relevance.reason(title, desc, **settings), 'excluded_technology', tech)
            self.assertIsNone(job_relevance.reason('Engineer', f'{tech} is required.'))

    def test_optional_excluded_technology_does_not_reject(self):
        for desc in ['Python is a nice-to-have.', 'Requirements:\nRuby\nNice to have:\nPython',
                     'Ruby backend. Python preferred.']:
            self.assertIsNone(job_relevance.reason('Ruby Engineer', desc, excluded_technologies=['Python']), desc)

    def test_accepted_target_alternative_does_not_reject(self):
        self.assertIsNone(job_relevance.reason('Engineer', 'Experience with Ruby or Python required.',
                                              target_technologies=['Ruby'], excluded_technologies=['Python']))

    def test_optional_target_alone_does_not_establish_fit(self):
        for desc in ['Ruby is a nice-to-have.', 'Nice to have:\nRuby']:
            self.assertEqual(job_relevance.reason('Engineer', desc, target_technologies=['Ruby']), 'stack_unknown')

    def test_literal_boundaries_preserve_punctuation_and_not_regex(self):
        for tech, text in [('Java', 'JavaScript'), ('Go', 'Django'), ('C', 'C++'), ('Node.js', 'NodeXjs'), ('Ruby.*', 'Ruby on Rails')]:
            self.assertFalse(job_relevance.matches(tech, text), (tech, text))
        for tech in ['C++', 'C#', '.NET', 'Node.js', 'Ruby on Rails']:
            self.assertTrue(job_relevance.matches(tech, 'Must use ' + tech + '.'), tech)

    def test_validation_rejects_malformed_and_conflicting_lists(self):
        for value in ['Python', None, [1], [''], ['x\ny'], ['x' * 101], ['a'] * 101]:
            with self.assertRaises(ValueError):
                job_relevance.validate_settings({'excluded_technologies': value})
        with self.assertRaises(ValueError):
            job_relevance.validate_settings({'target_technologies': [' Ruby '], 'excluded_technologies': ['ruby']})
        job_relevance.validate_settings({})

    def test_exclusion_records_exact_user_term_and_evidence(self):
        result = job_relevance.assess('Backend Engineer', 'Requirements:\nArbitraryStack is required.', excluded_technologies=['ArbitraryStack'])
        self.assertEqual(result['technology'], 'ArbitraryStack')
        self.assertEqual(result['evidence'], 'ArbitraryStack is required.')
        self.assertEqual(result['source'], 'requirement')

    def test_inline_requirement_heading_is_not_missed(self):
        self.assertEqual(job_relevance.reason('Engineer', 'Requirements: CustomStack', excluded_technologies=['CustomStack']), 'excluded_technology')
        self.assertIsNone(job_relevance.reason('Engineer', 'Nice to have: CustomStack', excluded_technologies=['CustomStack']))

    def test_optional_clause_does_not_hide_separate_mandatory_exclusion(self):
        self.assertEqual(job_relevance.reason('Engineer', 'Python required, Ruby preferred.',
                                             excluded_technologies=['Python']), 'excluded_technology')
        self.assertIsNone(job_relevance.reason('Engineer', 'Experience with Python, Ruby or Java required.',
                                              target_technologies=['Ruby'], excluded_technologies=['Python']))

    def test_relevance_module_has_no_candidate_country_policy(self):
        self.assertFalse(hasattr(job_relevance, 'argentina_location_reason'))


class RecheckTests(SafetyTest):
    def setUp(self):
        super().setUp()
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory(prefix='job-rechecks-')))
        self.enterContext(patch.object(apply, 'HOME', self.home))
        self.enterContext(patch.object(apply, 'APPLIED_FILE', self.home / 'applied.json'))
        self.enterContext(patch.object(apply, 'LOG_FILE', self.home / 'applications.csv'))
        self.enterContext(patch.object(apply, 'MANUAL_FILE', self.home / 'manual_apply.csv'))
        self.enterContext(patch.object(apply, 'RECHECK_STATE', {'id': 'test-run', 'count': 0}))
        apply.SETTINGS['recheck_enabled'] = True
        self.history = {'job': {'status': 'metadata_manual', 'title': 'Old title', 'company': 'Old company', 'date': '2026-10-21'}}
        apply.write_applied(self.history)

    def test_migration_backs_up_and_preserves_original_decision(self):
        before = apply.APPLIED_FILE.read_bytes()
        apply.begin_job_rechecks(self.history)
        backups = list((self.home / 'data' / 'backups').glob('*/applied.json'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), before)
        self.assertEqual(self.history['job']['status'], 'metadata_manual')
        self.assertEqual(self.history['job']['decisions'][0]['title'], 'Old title')

    def test_reassessment_preserves_prior_title_company_and_reason(self):
        apply.begin_job_rechecks(self.history)
        self.assertTrue(apply.should_visit_job(self.history, 'job'))
        apply.record(self.history, 'job', 'Correct title', 'Correct company', 'title_not_relevant')
        row = apply.load_applied()['job']
        self.assertEqual(row['decisions'][0]['title'], 'Old title')
        self.assertEqual(row['decisions'][0]['reason'], 'metadata_manual')
        self.assertEqual(row['title'], 'Correct title')
        self.assertIn('evaluated_at', row)
        self.assertEqual(row['recheck']['count'], 1)

    def test_submission_states_never_become_recheckable(self):
        for status in ['applied', 'applied_external', 'submission_pending', 'submission_unconfirmed',
                       'submission_rejected', 'skipped_unconfirmed', 'applied_previously']:
            value = {'status': status}
            self.assertFalse(job_rechecks.claim(value, 'revision', {'id': 'run', 'count': 0}), status)
            self.assertEqual(value, {'status': status})

    def test_manual_statuses_are_not_globally_retryable(self):
        self.assertFalse(apply.can_retry('metadata_manual'))
        self.assertFalse(apply.can_retry('title_not_relevant'))
        self.assertTrue(apply.can_retry('evaluation_passed'))

    def test_configured_exclusion_saves_evidence_without_stale_diagnostic(self):
        apply.SETTINGS['excluded_technologies'] = ['CustomStack']
        apply.record_relevance_decision(self.history, 'job', 'Engineer', 'Example', 'excluded_technology',
                                        'https://example.test', 'Requirements:\nCustomStack')
        diagnostic = self.history['job']['technology_diagnostic']
        self.assertEqual(diagnostic['technology'], 'CustomStack')
        self.assertEqual(self.history['job']['decisions'][-1]['technology_diagnostic'], diagnostic)
        apply.record(self.history, 'job', 'Engineer', 'Example', 'evaluation_passed')
        self.assertNotIn('technology_diagnostic', self.history['job'])
        self.assertEqual(self.history['job']['decisions'][-2]['technology_diagnostic'], diagnostic)

    def test_disabled_rechecks_and_failed_backup_do_not_unlock_history(self):
        apply.SETTINGS['recheck_enabled'] = False
        self.assertFalse(apply.should_visit_job(self.history, 'job'))
        with patch.object(job_rechecks, 'backup', side_effect=OSError('backup unavailable')):
            with self.assertRaises(OSError):
                job_rechecks.prepare(self.history, [apply.APPLIED_FILE], self.home / 'backups')
        self.assertNotIn('decisions', self.history['job'])

    def test_direct_processing_never_overwrites_submission_or_unclaimed_history(self):
        from test_answer_safety import node
        for status in ['applied', 'submission_pending', 'submission_unconfirmed', 'metadata_manual']:
            state = {'job': {'status': status, 'title': 'Original'}}
            page = node()
            self.assertFalse(apply.process_job(page, state, 'job'))
            self.assertEqual(state, {'job': {'status': status, 'title': 'Original'}})
            page.goto.assert_not_called()


    def test_recheck_requires_passed_gates_before_submit_is_enabled(self):
        apply.begin_job_rechecks(self.history)
        self.assertTrue(apply.should_visit_job(self.history, 'job'))
        self.assertEqual(self.history['job']['status'], 'metadata_manual')
        apply.mark_evaluation_passed(self.history, 'job', 'Engineer', 'Company', 'https://example.test')
        self.assertEqual(self.history['job']['status'], 'evaluation_passed')

    def test_rechecks_are_once_per_run_with_global_and_per_job_caps(self):
        now = dt.datetime.now(dt.timezone.utc)
        value = {'status': 'title_not_relevant'}
        state = {'id': 'run1', 'count': 0}
        self.assertTrue(job_rechecks.claim(value, 'r1', state, now=now))
        self.assertFalse(job_rechecks.claim(value, 'r2', state, now=now))
        self.assertFalse(job_rechecks.claim(value, 'r1', {'id': 'run2', 'count': 0}, now=now))
        self.assertTrue(job_rechecks.claim(value, 'r2', {'id': 'run2', 'count': 0}, now=now))
        self.assertTrue(job_rechecks.claim(value, 'r3', {'id': 'run3', 'count': 0}, now=now))
        self.assertFalse(job_rechecks.claim(value, 'r4', {'id': 'run4', 'count': 0}, now=now))
        self.assertFalse(job_rechecks.claim({'status': 'metadata_manual'}, 'r', {'id': 'run', 'count': 10}))

    def test_invalid_recheck_limits_are_rejected_without_changing_decision(self):
        value = {'status': 'metadata_manual'}
        for options in [{'max_per_run': True}, {'max_per_job': 999999}, {'cooldown_hours': -1}]:
            with self.assertRaises(ValueError):
                job_rechecks.claim(value, 'r', {'id': 'run', 'count': 0}, **options)
        self.assertEqual(value, {'status': 'metadata_manual'})


class ProfileWiringTests(SafetyTest):
    def test_legacy_profile_never_adds_technology_rules_or_rewrites_gui_titles(self):
        import cv_profile
        prof = cv_profile.empty_profile()
        prof.update(job_titles=['Python Engineer'], country='Argentina')
        apply.SETTINGS['relevance_profile'] = 'rails_backend'
        apply.apply_cv_profile(prof)
        self.assertEqual(apply.technology_filters(), {'target_technologies': [], 'excluded_technologies': []})
        self.assertIsNone(apply.job_ok('Python Engineer', 'Example', 'Must use Python and Django.'))
        self.assertEqual(apply.CFG['search']['keywords'], ['Python Engineer'])

    def test_technology_rules_change_revision_and_legacy_skips_are_bounded_recheckable(self):
        first = job_rechecks.revision(apply.CFG, apply.SETTINGS)
        apply.SETTINGS['excluded_technologies'] = ['CustomStack']
        self.assertNotEqual(first, job_rechecks.revision(apply.CFG, apply.SETTINGS))
        for status in ['required_stack_incompatible', 'specialized_role', 'excluded_technology']:
            value = {'status': status}
            run = {'id': 'run', 'count': 0}
            self.assertTrue(job_rechecks.claim(value, 'revision', run))
            self.assertFalse(job_rechecks.claim(value, 'revision', run))

    def test_user_regions_are_preserved_verbatim_after_cv_application(self):
        import cv_profile
        regions = ['North America', 'Latin America', 'Europe']
        apply.SETTINGS['locations'] = list(regions)
        prof = cv_profile.empty_profile()
        prof.update(job_titles=['Senior Ruby Developer'], country='Argentina')
        apply.apply_cv_profile(prof)
        self.assertEqual(apply.CFG['search']['locations'], regions)
        self.assertEqual(apply.SETTINGS['locations'], regions)
        self.assertIsNot(apply.CFG['search']['locations'], apply.SETTINGS['locations'])
        self.assertFalse(apply.job_location_ok('Argentina'))
        self.assertTrue(apply.job_location_ok('Remote - Europe'))

    def test_missing_settings_use_config_but_empty_settings_never_fall_back(self):
        apply.CFG['search']['locations'] = ['User-defined region']
        self.assertEqual(apply.selected_locations(), ['User-defined region'])
        for invalid in [[], [''], None, 'Europe']:
            apply.SETTINGS['locations'] = invalid
            with self.assertRaises(ValueError):
                apply.selected_locations()

    def test_linkedin_searches_each_user_region_without_personalized_feeds(self):
        from urllib.parse import urlparse, parse_qs
        regions = ['North America', 'Latin America', 'Europe', 'Custom region & area']
        apply.SETTINGS['locations'] = regions
        apply.PROFILE['country'] = 'Argentina'
        apply.CFG['search'].update(keywords=['Ruby Developer'], pages_per_search=1)
        with patch.object(apply.linkedin_search, 'LinkedInSearch') as factory:
            factory.return_value.search_page.return_value = {'ids': ['123'], 'has_next': False}
            self.assertEqual(list(apply.job_lists(object())), [['123']] * len(regions))
        self.assertEqual([call.args[1] for call in factory.return_value.search_page.call_args_list], regions)
        urls = [apply.search_url('Ruby Developer', region) for region in regions]
        self.assertEqual([parse_qs(urlparse(url).query)['location'][0] for url in urls], regions)
        self.assertTrue(all(urlparse(url).path == '/jobs/search/' for url in urls))

    def test_rails_search_and_confirmed_experience_do_not_use_generic_role_words_alone(self):
        import cv_profile
        prof = cv_profile.empty_profile()
        prof.update(job_titles=TARGET_ROLES, total_years=17, country='Argentina')
        apply.SETTINGS.update(target_technologies=['Ruby'], confirmed_skill_years={'Ruby': 14, 'Ruby on Rails': 14})
        apply.apply_cv_profile(prof)
        self.assertEqual(apply.CFG['search']['keywords'], TARGET_ROLES)
        self.assertEqual(apply.answer('Years of Ruby experience?', numeric=True), ['14'])
        self.assertEqual(apply.job_ok('Senior Software Engineer', 'Example', ''), 'stack_unknown')
        self.assertIsNone(apply.job_ok('Senior Ruby on Rails Developer', 'Example', 'Ruby on Rails backend'))


@unittest.skipUnless(os.getenv('AUTOAPPLY_BROWSER_TESTS') == '1', 'optional local Chrome current-LinkedIn fixtures')
class BrowserLinkedinLayoutTests(SafetyTest):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.pw = sync_playwright().start()
        cls.addClassCleanup(cls.pw.stop)
        cls.browser = cls.pw.chromium.launch(channel='chrome', headless=True)
        cls.addClassCleanup(cls.browser.close)

    def setUp(self):
        super().setUp()
        self.context = self.browser.new_context()
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.html = ''
        self.context.route('**/*', lambda route: route.fulfill(content_type='text/html', body=self.html))

    def load(self, title='Senior Ruby on Rails Developer', company='Proxify', tab=None, date='Reposted 2 days ago'):
        self.html = f'''<html lang="en"><meta charset="utf-8"><title>{tab or title + ' | ' + company + ' | LinkedIn'}</title>
            <div id="JobDetails_ManageJobBanner_123"></div><section class="hashed-new-card">
            <a href="https://www.linkedin.com/company/example/">{company}</a><p>{title}</p>
            <p>Argentina · {date} · 20 applicants</p><p>Remote</p></section>
            <div id="JobDetails_AboutTheJob_123"><h2>About the job</h2>
            <div data-testid="expandable-text-box">Build SaaS with Ruby on Rails and PostgreSQL.</div></div>
            <aside><h1>Unrelated recommendation</h1>Posted 1 hour ago</aside></html>'''
        self.page.goto('https://www.linkedin.com/jobs/view/123/')

    def test_new_dom_without_h1_classes_or_jsonld_supplies_date_and_identity(self):
        self.load()
        title, company, desc = apply.job_info(self.page)
        self.assertEqual((title, company), ('Senior Ruby on Rails Developer', 'Proxify'))
        self.assertIn('Ruby on Rails', desc)
        metadata = job_metadata.page_metadata(self.page, 'linkedin', self.page.url)
        self.assertIsNotNone(metadata['posted_at'])
        self.assertEqual(metadata['workplace'], 'remote')
        self.assertEqual(metadata['location'], 'Argentina')

    def test_both_quintoandar_titles_are_not_tab_order_guesses(self):
        for role in ['Staff Software Engineer', 'Senior Software Engineer']:
            self.load(title='Grupo QuintoAndar | ' + role, company='QuintoAndar',
                      tab='Grupo QuintoAndar | ' + role + ' | QuintoAndar | LinkedIn')
            self.assertEqual(apply.job_info(self.page)[:2], (role, 'QuintoAndar'))

    def test_missing_date_reports_diagnostic_not_recommendation_date(self):
        self.load(date='Date unavailable')
        metadata = job_metadata.page_metadata(self.page, 'linkedin', self.page.url)
        self.assertIsNone(metadata['posted_at'])
        self.assertIn('No parseable date', metadata['diagnostic']['reason'])

    def test_late_hydration_is_waited_for(self):
        self.html = '''<script>setTimeout(()=>document.body.innerHTML='<div id="JobDetails_ManageJobBanner_123"></div><section>Remote<br>Posted 2 days ago</section>', 250)</script>'''
        self.page.goto('https://www.linkedin.com/jobs/view/123/')
        self.assertIsNotNone(job_metadata.page_metadata(self.page, 'linkedin', self.page.url)['posted_at'])

    def test_title_arriving_before_description_does_not_trigger_false_stack_unknown(self):
        self.load()
        self.html = self.html.replace('Build SaaS with Ruby on Rails and PostgreSQL.', '')
        self.html += '''<script>setTimeout(()=>document.querySelector('[data-testid="expandable-text-box"]').textContent='Build with Ruby on Rails',400)</script>'''
        self.page.goto('https://www.linkedin.com/jobs/view/123/')
        title, company, description = apply.job_info(self.page)
        self.assertIn('Ruby on Rails', description)

    def test_delayed_date_in_an_already_visible_header_is_waited_for(self):
        self.html = '''<div id="JobDetails_ManageJobBanner_123"></div><section>Remote<p id="date"></p></section>
            <script>setTimeout(()=>document.getElementById('date').textContent='Posted 2 days ago',250)</script>'''
        self.page.goto('https://www.linkedin.com/jobs/view/123/')
        self.assertIsNotNone(job_metadata.page_metadata(self.page, 'linkedin', self.page.url)['posted_at'])

    def test_matching_structured_identity_is_a_verified_fallback(self):
        url = 'https://www.linkedin.com/jobs/view/123/'
        self.html = '<script type="application/ld+json">' + json.dumps({'@type': 'JobPosting',
            'url': '/jobs/view/123/', 'title': 'Senior Ruby Developer', 'hiringOrganization': {'name': 'Example'},
            'description': 'Ruby backend', 'datePosted': '2026-10-20'}) + '</script>'
        self.page.goto(url)
        self.assertEqual(apply.job_info(self.page)[:2], ('Senior Ruby Developer', 'Example'))

    def test_relative_url_for_other_posting_does_not_supply_freshness(self):
        self.html = '<h1>Senior Ruby Developer</h1><script type="application/ld+json">' + json.dumps({'@type': 'JobPosting',
            'url': '/jobs/view/other/', 'title': 'Senior Ruby Developer', 'datePosted': '2026-10-20'}) + '</script>'
        self.page.goto('https://www.linkedin.com/jobs/view/123/')
        self.assertIsNone(job_metadata.page_metadata(self.page, 'linkedin', self.page.url)['posted_at'])

    def test_suitable_rails_pipeline_fills_local_form_without_submit(self):
        self.load()
        self.html += '''<script>window.sent=0</script><button type="button">Easy Apply</button><div role="dialog">
            <form onsubmit="event.preventDefault();window.sent++"><label for="years">Years of Ruby experience?</label>
            <input id="years" type="number" required><footer><button type="submit">Submit application</button></footer></form></div>'''
        apply.SETTINGS['relevance_profile'] = 'rails_backend'
        apply.CFG['skill_years'] = {'ruby': 14}
        with patch.object(apply, 'DRY_RUN', True):
            self.assertTrue(apply.process_job(self.page, {}, '123'))
        self.assertEqual(self.page.locator('#years').input_value(), '14')
        self.assertEqual(self.page.evaluate('window.sent'), 0)

    def test_wellfound_uses_only_verified_job_description_not_recommendations(self):
        import sources
        self.enterContext(patch.object(sources, 'core', apply))
        self.enterContext(patch.object(apply, 'load_applied', return_value={}))
        self.enterContext(patch.object(sources, 'goto', side_effect=lambda page, url, name: (page.goto(url), True)[1]))
        url = 'https://wellfound.com/jobs/123-engineer'
        self.html = '<title>Engineer at Example • Remote</title><div class="select__control"><span class="select__single-value">Engineer</span><input id="react-select-1-input"></div><a href="/jobs/123-engineer">Job</a>'
        self.html += '<aside>Recommendations: Python is required.</aside><script type="application/ld+json">' + json.dumps({
            '@type': 'JobPosting', 'url': url, 'title': 'Engineer', 'description': 'Ruby backend.',
            'datePosted': '2026-10-20', 'jobLocationType': 'TELECOMMUTE'}) + '</script>'
        apply.CFG['search']['keywords'] = ['Engineer']
        job, _ = next(sources._wellfound_public(self.page))
        self.assertEqual(job['desc'], 'Ruby backend.')
        self.assertIsNone(job_relevance.reason(job['title'], job['desc'], excluded_technologies=['Python']))

    def test_wellfound_without_scoped_description_does_not_fall_back_to_body(self):
        import sources
        self.enterContext(patch.object(sources, 'core', apply))
        self.enterContext(patch.object(apply, 'load_applied', return_value={}))
        self.enterContext(patch.object(sources, 'goto', side_effect=lambda page, url, name: (page.goto(url), True)[1]))
        self.html = '<title>Engineer at Example • Remote</title><div class="select__control"><span class="select__single-value">Engineer</span><input id="react-select-1-input"></div><a href="/jobs/123-engineer">Job</a><aside>Ruby backend; Python is required.</aside>'
        apply.CFG['search']['keywords'] = ['Engineer']
        job, _ = next(sources._wellfound_public(self.page))
        self.assertEqual(job['desc'], '')
        self.assertEqual(job_relevance.reason(job['title'], job['desc'], target_technologies=['Ruby'], excluded_technologies=['Python']), 'stack_unknown')
