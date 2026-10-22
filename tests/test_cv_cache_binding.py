"""Follow-up #4: a cached profile belongs to CV contents, never just a filename."""
import base64
import copy
import hashlib
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from test_answer_safety import SafetyTest
import apply
import cv_profile
import gui


def profile(name='Old'):
    result = cv_profile.empty_profile()
    result.update(first_name=name, last_name='User', email='test@example.test', phone_intl='+1 555 123 4567',
        city='Toronto', country='Canada', total_years=3, job_titles=['Engineer'], current_title=f'{name} title',
        title_keywords=[f'{name.lower()}-keyword'], skills=[{'skill': name, 'years': 3}])
    return result


class CVCacheFixture(SafetyTest):
    def setUp(self):
        super().setUp()
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory(prefix='cv-cache-binding-')))
        (self.home / 'cv').mkdir()
        self.resume = self.home / 'cv' / 'resume.pdf'
        self.resume.write_bytes(b'old resume fixture')
        self.old_hash = hashlib.sha256(self.resume.read_bytes()).hexdigest()
        self.old = profile()
        apply.SETTINGS.update(resume=str(self.resume), cv_profile=self.old, cv_hash=self.old_hash, notice='14')
        self.enterContext(patch.object(apply, 'HOME', self.home))
        self.enterContext(patch.object(apply, 'SETTINGS_FILE', self.home / 'settings.json'))
        self.enterContext(patch.object(apply, 'DRY_RUN', False))
        self.enterContext(patch.object(gui, 'STATE', {'running': False, 'analyzing': False}))
        self.enterContext(patch.object(gui, 'ACTIVITY_LOCK', threading.Lock()))
        self.enterContext(patch.object(apply, 'find_key', return_value='test-key'))
        self.thread = self.enterContext(patch.object(gui.threading, 'Thread'))

    def upload(self, content=b'new resume fixture', name='new.pdf'):
        return gui.save_cv({'name': name, 'data': base64.b64encode(content).decode()})

    def body(self, digest=None):
        visible = {k: v for k, v in profile('New').items() if k not in ('current_title', 'title_keywords', 'skills')}
        current_hash = hashlib.sha256(Path(apply.SETTINGS['resume']).read_bytes()).hexdigest()
        return {'profile': visible, 'cv_hash': digest if digest is not None else current_hash,
                'english': '3', 'notice': '14', 'locations': ['Worldwide'], 'sources': ['linkedin'],
                'test_mode': True, 'currency': 'USD', 'salary': '', 'salary_eur': '', 'salary_usd': ''}

    def assert_no_cache(self):
        self.assertNotIn('cv_profile', apply.SETTINGS)
        self.assertNotIn('cv_hash', apply.SETTINGS)


class CVCacheBindingTests(CVCacheFixture):
    def test_new_cv_invalidates_old_profile_and_hash(self):
        result = self.upload()
        self.assertTrue(result['ok'])
        self.assert_no_cache()
        self.assertIsNone(gui.state()['profile'])
        self.assertTrue(result['profile_reset'])

    def test_overwriting_same_filename_invalidates_old_profile(self):
        self.upload(name='resume.pdf')
        self.assert_no_cache()

    def test_same_contents_under_new_name_preserve_matching_cache(self):
        result = self.upload(self.resume.read_bytes(), 'renamed.pdf')
        self.assertEqual(apply.SETTINGS['cv_profile'], self.old)
        self.assertEqual(apply.SETTINGS['cv_hash'], self.old_hash)
        self.assertFalse(result['profile_reset'])

    def test_unchanged_upload_preserves_cache_and_reviewed_values(self):
        result = self.upload(self.resume.read_bytes(), 'resume.pdf')
        self.assertFalse(result['profile_reset'])
        self.assertEqual(gui.state()['profile'], self.old)

    def test_external_change_is_not_served_as_matching_cache(self):
        self.resume.write_bytes(b'external replacement')
        state = gui.state()
        self.assertIsNone(state['profile'])
        self.assertNotEqual(state['cv_hash'], self.old_hash)

    def test_missing_resume_does_not_serve_a_cached_profile(self):
        self.resume.unlink()
        self.assertIsNone(gui.state()['profile'])

    def test_legacy_cache_without_hash_is_not_reused(self):
        apply.SETTINGS.pop('cv_hash')
        self.assertIsNone(gui.state()['profile'])
        self.assertEqual(gui.start(self.body()), {'ok': True})
        self.assertEqual(apply.SETTINGS['cv_profile']['current_title'], '')

    def test_external_change_cannot_inherit_hidden_fields_at_start(self):
        self.resume.write_bytes(b'external replacement')
        self.assertEqual(gui.start(self.body()), {'ok': True})
        for key, expected in [('current_title', ''), ('title_keywords', []), ('skills', [])]:
            self.assertEqual(apply.SETTINGS['cv_profile'][key], expected)

    def test_manual_profile_after_upload_has_no_old_hidden_data(self):
        self.upload()
        self.assertEqual(gui.start(self.body()), {'ok': True})
        self.assertEqual(apply.PROFILE['current_title'], '')
        self.assertEqual(apply.SETTINGS['cv_profile']['title_keywords'], [])
        self.assertEqual(apply.SETTINGS['cv_hash'], gui.state()['cv_hash'])

    def test_same_cv_preserves_valid_hidden_fields(self):
        self.assertEqual(gui.start(self.body()), {'ok': True})
        self.assertEqual(apply.SETTINGS['cv_profile']['current_title'], 'Old title')

    def test_stale_tab_cannot_start_after_external_change(self):
        body = self.body(self.old_hash)
        self.resume.write_bytes(b'external replacement')
        before = copy.deepcopy(apply.SETTINGS)
        self.assertEqual(gui.start(body)['error'], 'cv_changed')
        self.assertEqual(apply.SETTINGS, before)
        self.thread.assert_not_called()
        self.assertFalse(gui.ACTIVITY_LOCK.locked())

    def test_missing_cv_identity_cannot_start(self):
        body = self.body()
        body.pop('cv_hash')
        self.assertEqual(gui.start(body)['error'], 'cv_changed')
        self.thread.assert_not_called()

    def test_failed_analysis_after_replacement_cannot_restore_old_profile(self):
        self.upload()
        with patch.object(cv_profile, 'extract', side_effect=ValueError('fixture failure')):
            self.assertEqual(gui.analyze({'key': ''})['error'], 'analyze_failed')
        self.assert_no_cache()
        self.assertIsNone(gui.state()['profile'])

    def test_matching_valid_cache_survives_failed_reanalysis(self):
        with patch.object(cv_profile, 'extract', side_effect=ValueError('fixture failure')):
            self.assertEqual(gui.analyze({'key': ''})['error'], 'analyze_failed')
        self.assertEqual(gui.state()['profile'], self.old)

    def test_file_changed_during_analysis_cannot_bind_old_result_to_new_file(self):
        def extract(*args, **kwargs):
            self.resume.write_bytes(b'changed during extraction')
            return profile('New')
        with patch.object(cv_profile, 'extract', side_effect=extract):
            self.assertEqual(gui.analyze({'key': ''})['error'], 'cv_changed')
        self.assert_no_cache()

    def test_analysis_of_new_cv_returns_and_caches_matching_identity(self):
        self.upload()
        with patch.object(cv_profile, 'extract', return_value=profile('New')):
            result = gui.analyze({'key': ''})
        self.assertTrue(result['ok'])
        self.assertEqual(result['cv_hash'], gui.state()['cv_hash'])
        self.assertEqual(apply.SETTINGS['cv_profile'], profile('New'))

    def test_external_change_before_queued_worker_starts_aborts_before_browser(self):
        self.assertEqual(gui.start(self.body()), {'ok': True})
        self.resume.write_bytes(b'changed while queued')
        with patch.object(apply, 'run_bot') as run:
            self.thread.call_args.kwargs['target']()
        run.assert_not_called()
        self.assertFalse(gui.STATE['running'])
        self.assertFalse(gui.ACTIVITY_LOCK.locked())

    def test_failed_upload_does_not_invalidate_matching_cache(self):
        before = copy.deepcopy(apply.SETTINGS)
        with patch.object(Path, 'write_bytes', side_effect=OSError('disk full')), self.assertRaises(OSError):
            self.upload()
        self.assertEqual(apply.SETTINGS, before)

    def test_unreadable_resume_does_not_serve_cached_profile(self):
        with patch.object(Path, 'read_bytes', side_effect=OSError('unreadable CV')):
            self.assertIsNone(gui.state()['profile'])

    def test_file_removed_during_analysis_invalidates_the_cache(self):
        def extract(*args, **kwargs):
            self.resume.unlink()
            return profile('New')
        with patch.object(cv_profile, 'extract', side_effect=extract):
            self.assertEqual(gui.analyze({'key': ''})['error'], 'cv_changed')
        self.assert_no_cache()

    def test_saved_cache_is_not_reused_after_external_change_and_reload(self):
        self.assertEqual(gui.start(self.body()), {'ok': True})
        with patch.object(apply, 'run_bot'):
            self.thread.call_args.kwargs['target']()
        self.resume.write_bytes(b'changed after the app saved settings')
        apply.SETTINGS.clear()
        apply.load_settings()
        self.assertIsNone(gui.state()['profile'])


@unittest.skipUnless(os.getenv('AUTOAPPLY_BROWSER_TESTS') == '1', 'optional local Chrome CV-identity UI tests')
class BrowserCVCacheTests(CVCacheFixture):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.pw = sync_playwright().start()
        cls.addClassCleanup(cls.pw.stop)
        cls.browser = cls.pw.chromium.launch(channel='chrome', headless=True)
        cls.addClassCleanup(cls.browser.close)

    def setUp(self):
        super().setUp()
        self.context = self.browser.new_context(locale='en-US')
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.page.set_default_timeout(2000)
        self.errors = []
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))
        self.extract = self.enterContext(patch.object(cv_profile, 'extract', return_value=profile('New')))
        self.starts = []
        def respond(route):
            path = route.request.url.split('autoapply.test', 1)[-1]
            if path == '/':
                route.fulfill(content_type='text/html', body=gui.PAGE.replace('__TOKEN__', gui.TOKEN))
            elif path == '/api/state':
                route.fulfill(json=gui.state())
            elif path == '/api/cv':
                route.fulfill(json=gui.save_cv(route.request.post_data_json))
            elif path == '/api/analyze':
                route.fulfill(json=gui.analyze(route.request.post_data_json))
            elif path == '/api/start':
                body = route.request.post_data_json
                self.starts.append(body)
                route.fulfill(json=gui.start(body))
            else:
                route.abort()
        self.context.route('**/*', respond)
        self.page.goto('http://autoapply.test/')
        self.page.wait_for_function("document.getElementById('first_name').value === 'Old'")

    def choose(self, content=b'new CV contents', name='resume.pdf'):
        with self.page.expect_response('**/api/cv'):
            self.page.locator('#file').set_input_files({'name': name, 'mimeType': 'application/pdf', 'buffer': content})

    def test_replacement_clears_ui_and_manual_cannot_resurrect_old_profile(self):
        self.choose()
        self.page.wait_for_function("document.getElementById('first_name').value === ''")
        self.page.locator('#manual').click()
        self.assertEqual(self.page.locator('#first_name').input_value(), '')
        self.assertEqual(self.page.locator('#job_titles').input_value(), '')
        self.assertIn('CV contents changed', self.page.locator('#m1').inner_text())
        self.assert_no_cache()
        self.assertEqual(self.errors, [])

    def test_unchanged_upload_preserves_edits_in_review_form(self):
        self.page.locator('#first_name').fill('Reviewed edit')
        self.choose(self.resume.read_bytes(), name='renamed.pdf')
        self.page.wait_for_function("document.getElementById('cvname').textContent === 'renamed.pdf'")
        self.assertEqual(self.page.locator('#first_name').input_value(), 'Reviewed edit')
        self.assertEqual(apply.SETTINGS['cv_hash'], self.old_hash)

    def test_failed_analysis_after_upload_keeps_manual_form_empty(self):
        self.choose()
        self.extract.side_effect = ValueError('fixture failure')
        self.page.locator('#analyze').click()
        self.page.locator('#m1').filter(has_text='Could not read the CV').wait_for()
        self.page.locator('#manual').click()
        self.assertEqual(self.page.locator('#first_name').input_value(), '')
        self.assert_no_cache()

    def test_new_analysis_updates_manual_profile_instead_of_restoring_old_state(self):
        self.choose()
        self.page.locator('#analyze').click()
        self.page.wait_for_function("document.getElementById('first_name').value === 'New'")
        self.page.locator('#first_name').fill('Temporary edit')
        self.page.locator('#manual').click()
        self.assertEqual(self.page.locator('#first_name').input_value(), 'New')
        self.assertEqual(apply.SETTINGS['cv_profile']['current_title'], 'New title')

    def test_stale_tab_start_is_rejected_and_visible_old_fields_are_cleared(self):
        self.resume.write_bytes(b'external replacement after the UI was opened')
        self.page.locator('#start').click()
        self.page.locator('#m3').filter(has_text='The CV changed').wait_for()
        self.assertEqual(self.starts[0]['cv_hash'], self.old_hash)
        self.assertEqual(self.page.locator('#first_name').input_value(), '')
        self.assertTrue(self.page.locator('#start').is_enabled())
        self.assertFalse(self.page.locator('#s4').is_visible())
        self.thread.assert_not_called()
        self.assertEqual(self.errors, [])
