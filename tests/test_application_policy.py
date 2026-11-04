import hashlib
from datetime import date, timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from test_answer_safety import SafetyTest
from test_resume_match import JD, CV, review
import apply
import application_policy as policy
from application_queue import Store
import candidate_workflow
import job_metadata
import linkedin_search
import policy_discovery


class PolicyTests(SafetyTest):
    def setUp(self):
        super().setUp()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        (self.folder / 'CV_Andre2026.pdf').write_bytes(b'local-cv')
        self.enterContext(patch.object(apply, 'ROOT', self.folder))
        self.enterContext(patch.object(apply, 'HOME', self.folder))
        apply.CFG['application_policy']['enabled'] = True
        self.enterContext(patch('cv_profile.cv_text', return_value=CV))
        self.enterContext(patch.object(apply, 'find_key', return_value=''))
        policy.activate(apply)
        self.job = dict(id='123', title='Engineer', company='Example', desc=JD,
                        url='https://www.linkedin.com/jobs/view/123/',
                        metadata={'location': 'Argentina', 'workplace': 'remote'})

    def test_approved_policy_replaces_legacy_filters_without_sending(self):
        self.assertEqual(apply.CFG['search']['keywords'], policy.TITLES)
        self.assertIsNone(apply.CFG['search']['pages_per_search'])
        self.assertIsNone(apply.CFG['search']['posted_within_hours'])
        self.assertEqual(apply.CFG['resume_match']['minimum_percent'], 70)
        self.assertEqual(apply.PROFILE['cover_letter'], policy.COVER_LETTER)
        self.assertEqual(Path(apply.SETTINGS['resume']), self.folder / 'CV_Andre2026.pdf')
        self.assertFalse((self.folder / 'data/applied.json').exists())
        apply.SETTINGS.update(locations=['India'], excluded_title_words=['Ruby'])
        self.assertNotIn('India', apply.selected_locations())
        self.assertIn('Brazil', apply.selected_locations())
        self.assertIn('Nigeria', apply.selected_locations())
        self.assertIn('New Zealand', apply.selected_locations())
        self.assertIsNone(apply.job_ok('Senior Ruby Engineer', 'Example', JD))

    def test_stale_root_profile_blocks_activation(self):
        apply.SETTINGS.update(cv_profile={}, cv_hash='stale')
        with self.assertRaisesRegex(ValueError, 'differs'):
            policy.activate(apply)

    def test_invalid_policy_switch_is_not_treated_as_permission(self):
        apply.CFG['application_policy']['enabled'] = 'false'
        with self.assertRaises(ValueError):
            policy.activate(apply)

    def test_geography_does_not_confuse_global_with_asia_only(self):
        for text in ['India', 'Singapore', 'Asia', 'Japan']:
            self.assertEqual(policy.geography(text), 'geography_excluded')
        for text in ['Worldwide including India', 'Global', 'Argentina', 'United States', 'Europe', 'Australia']:
            self.assertIsNone(policy.geography(text))
        for text in ['', 'EMEA', 'APAC', 'Unknown city']:
            self.assertIn(policy.geography(text), ('geography_unknown', 'geography_ambiguous'))
        self.assertEqual(policy.geography('Georgia'), 'geography_ambiguous')
        self.assertIsNone(policy.geography('Atlanta, Georgia, United States'))
        self.assertIsNone(policy.geography('LATAM'))
        self.assertIsNone(policy.geography('US'))

    def test_no_key_saves_full_jd_as_needs_assessment_not_rejected(self):
        entry = candidate_workflow.evaluate(apply, self.job)
        self.assertEqual(entry['status'], 'needs_assessment')
        self.assertIsNone(entry['assessment']['score'])
        self.assertIn('No OpenRouter key', entry['assessment']['diagnostic'])
        self.assertEqual(entry['description'], JD)
        self.assertEqual(entry['resume_sha256'], hashlib.sha256(b'local-cv').hexdigest())
        self.assertEqual(candidate_workflow.store(apply).load()['jobs']['123']['description'], JD)

    def test_manual_quoted_review_enters_queue_at_seventy(self):
        candidate_workflow.evaluate(apply, self.job)
        result = candidate_workflow.apply_review(apply, '123', review())
        self.assertEqual(result['status'], 'eligible')
        self.assertEqual(result['assessment']['score'], 70)
        self.assertTrue(result['qualifies'])
        self.assertFalse((self.folder / 'data/applied.json').exists())

    def test_per_run_manual_assessment_mode_does_not_call_api(self):
        with patch.dict('os.environ', {'AUTOAPPLY_ASSESSMENT_MODE': 'manual'}), \
                patch.object(apply, 'current_llm_config') as config, patch.object(apply, 'find_key') as key:
            entry = candidate_workflow.evaluate(apply, self.job)
            self.assertEqual(entry['status'], 'needs_assessment')
            self.assertIn('requested for this run', entry['assessment']['diagnostic'])
            config.assert_not_called()
            key.assert_not_called()

    def test_high_score_retains_legal_blocker(self):
        self.job['desc'] += ' US authorization required.'
        candidate_workflow.evaluate(apply, self.job)
        value = review()
        value['blockers'] = [dict(kind='legal', jd_quote='US authorization required.',
                                  verdict='unknown', explanation='User confirmation required')]
        result = candidate_workflow.apply_review(apply, '123', value)
        self.assertEqual(result['status'], 'eligible_blocked')
        self.assertTrue(result['qualifies'])

    def test_submit_limit_preserves_candidate_without_opening_form(self):
        with patch.object(apply, 'submission_quota_status', return_value='skipped_source_limit'), \
                patch.object(apply, 'job_info', return_value=('Engineer', 'Example', JD)), \
                patch.object(apply, 'read_job_metadata', return_value=self.job['metadata']), \
                patch.object(apply, 'check_resume_match', return_value=True), \
                patch.object(apply, 'check_job_eligibility', return_value=True), \
                patch.object(apply, 'apply_to_job') as opener:
            self.assertFalse(apply.process_job(MagicMock(), {}, '123'))
            opener.assert_not_called()
        self.assertEqual(candidate_workflow.store(apply).load()['jobs']['123']['status'], 'eligible_deferred_limit')

    def test_no_age_cutoff_still_checks_remote(self):
        self.assertIsNone(job_metadata.eligibility_reason({'workplace': 'remote'}, None))
        self.assertEqual(job_metadata.eligibility_reason({'workplace': 'hybrid'}, None), 'not_remote')
        self.assertEqual(job_metadata.eligibility_reason({}, None), 'workplace_unknown')

    def test_final_submit_guard_blocks_unreviewed_candidate(self):
        with patch.object(apply, 'DRY_RUN', False), patch.object(apply, 'STOP') as stop:
            stop.is_set.return_value = False
            primary = MagicMock()
            self.assertEqual(apply.submit_application(MagicMock(), primary, '123', 'Engineer', 'Example',
                             self.job['url'], MagicMock(), {}), 'manual_review')
            primary.click.assert_not_called()

    def test_final_submit_guard_rejects_changed_cv_or_unresolved_blockers(self):
        candidate_workflow.evaluate(apply, self.job)
        candidate_workflow.apply_review(apply, '123', review())
        self.assertTrue(candidate_workflow.submission_allowed(apply, '123'))
        candidate_workflow.store(apply).put('123', blockers=[{'kind': 'legal'}])
        self.assertFalse(candidate_workflow.submission_allowed(apply, '123'))
        candidate_workflow.store(apply).put('123', blockers=[])
        (self.folder / 'CV_Andre2026.pdf').write_bytes(b'replaced-cv')
        self.assertFalse(candidate_workflow.submission_allowed(apply, '123'))

    def test_account_cooldown_blocks_all_roles_without_model_call(self):
        retry_after = (date.today() + timedelta(days=180)).isoformat()
        candidate_workflow.store(apply).account('proxify', status='cooldown', retry_after=retry_after,
                                               reason='User reported shared-portal rejection cooldown')
        with patch.object(apply, 'current_llm_config') as model:
            for job_id, title in [('1', 'Senior Rails Developer'), ('2', 'Fullstack Rails Developer')]:
                job = {**self.job, 'id': job_id, 'title': title, 'company': 'Proxify'}
                entry = candidate_workflow.evaluate(apply, job)
                self.assertEqual(entry['status'], 'account_cooldown')
                self.assertEqual(entry['retry_after'], retry_after)
                self.assertFalse(candidate_workflow.submission_allowed(apply, job_id))
            model.assert_not_called()

    def test_duplicate_warning_blocks_retry_without_claiming_a_submission(self):
        path = self.folder / 'data/application_observations.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'version': 1, 'jobs': {'123': {
            'kind': 'possible_duplicate', 'message': 'Maybe already applied',
            'prior_submission_confirmed': False, 'new_application_sent_by_agent': False}}}), encoding='utf-8')
        with patch.object(apply, 'current_llm_config') as model:
            entry = candidate_workflow.evaluate(apply, self.job)
            self.assertEqual(entry['status'], 'possible_duplicate')
            self.assertFalse(candidate_workflow.submission_allowed(apply, '123'))
            self.assertFalse((self.folder / 'data/applied.json').exists())
            model.assert_not_called()


class QueueTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = Store(Path(temp.name) / 'candidates.json')

    def test_confirmed_outcome_cannot_regress(self):
        self.store.put('123', status='applied', title='Engineer')
        self.store.put('123', status='needs_assessment')
        self.store.put('123', status='submission_pending')
        self.assertEqual(self.store.load()['jobs']['123']['status'], 'applied')

    def test_pending_cannot_become_eligible_again(self):
        self.store.put('123', status='submission_pending')
        self.store.put('123', status='eligible')
        self.assertEqual(self.store.load()['jobs']['123']['status'], 'submission_pending')

    def test_corrupt_queue_never_overwritten(self):
        self.store.path.write_text('{broken', encoding='utf-8')
        with self.assertRaises(ValueError):
            self.store.put('123', status='eligible')
        self.assertEqual(self.store.path.read_text(), '{broken')

    def test_private_atomic_utf8_roundtrip(self):
        self.store.put('123', title='Разработчик', status='eligible')
        self.assertEqual(self.store.load()['jobs']['123']['title'], 'Разработчик')
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)


class DiscoveryTests(PolicyTests):
    def test_saved_candidates_are_resumed_even_when_discovery_completed(self):
        store = candidate_workflow.store(apply)
        store.put('123', status='needs_assessment')
        key = policy_discovery.key_for('Engineer', 'Argentina')
        store.progress(key, status='complete', next_page=2)
        with patch.object(policy, 'TITLES', ['Engineer']), patch.object(policy, 'LOCATIONS', ['Argentina']), \
                patch.object(linkedin_search, 'PolicySearch') as search:
            self.assertEqual(list(policy_discovery.job_lists(apply, MagicMock())), [['123']])
            search.return_value.search_page.assert_not_called()

    def test_all_pages_beyond_previous_two_page_cap_are_processed(self):
        search = MagicMock()
        search.search_page.side_effect = [dict(ids=[str(i)], has_next=i < 3) for i in range(4)]
        search.page.url = 'https://www.linkedin.com/jobs/search-results/'
        with patch.object(policy, 'TITLES', ['Engineer']), patch.object(policy, 'LOCATIONS', ['Argentina']), \
                patch.object(linkedin_search, 'PolicySearch', return_value=search):
            self.assertEqual(list(policy_discovery.job_lists(apply, MagicMock())), [['0'], ['1'], ['2'], ['3']])
        progress = next(iter(candidate_workflow.store(apply).load()['searches'].values()))
        self.assertEqual(progress['status'], 'complete')
        self.assertEqual(progress['next_page'], 4)

    def test_interrupted_page_is_not_marked_processed(self):
        search = MagicMock()
        search.search_page.return_value = dict(ids=['123'], has_next=True)
        search.page.url = 'https://www.linkedin.com/jobs/search-results/'
        with patch.object(policy, 'TITLES', ['Engineer']), patch.object(policy, 'LOCATIONS', ['Argentina']), \
                patch.object(linkedin_search, 'PolicySearch', return_value=search):
            batches = policy_discovery.job_lists(apply, MagicMock())
            self.assertEqual(next(batches), ['123'])
            batches.close()
        progress = next(iter(candidate_workflow.store(apply).load()['searches'].values()))
        self.assertEqual(progress['status'], 'processing')
        self.assertEqual(progress['next_page'], 0)

    def test_unsupported_region_does_not_stop_other_regions(self):
        search = MagicMock()
        search.search_page.side_effect = [linkedin_search.UnsupportedLocation('not supported'),
                                        dict(ids=['123'], has_next=False)]
        search.page.url = 'https://www.linkedin.com/jobs/search-results/'
        with patch.object(policy, 'TITLES', ['Engineer']), patch.object(policy, 'LOCATIONS', ['Worldwide', 'Argentina']), \
                patch.object(linkedin_search, 'PolicySearch', return_value=search):
            self.assertEqual(list(policy_discovery.job_lists(apply, MagicMock())), [['123']])
        states = candidate_workflow.store(apply).load()['searches'].values()
        self.assertEqual({s['status'] for s in states}, {'blocked', 'complete'})
