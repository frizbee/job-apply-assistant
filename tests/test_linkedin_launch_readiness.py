"""LinkedIn follow/upload widgets, stalled forms and honest deferred categories."""
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import apply
import deferred_review
import gui
import linkedin_forms
from test_answer_safety import SafetyTest


class QueueRecoveryTests(SafetyTest):
    def test_legacy_unanswered_and_step_limits_are_deferred_without_rewriting_history(self):
        missing = Path('missing-manual-fixture.csv')
        history = {str(i): {'status': status, 'title': 'Engineer', 'company': 'Example', 'url': 'https://www.linkedin.com/jobs/view/123/'}
                   for i, status in enumerate(['skipped_unanswered', 'skipped_too_many_steps', 'skipped_external_limit',
                                               'submission_pending', 'submission_unconfirmed', 'applied'])}
        with patch.object(apply, 'load_applied', return_value=history), patch.object(apply, 'MANUAL_FILE', missing):
            result = gui.deferred_jobs()
        self.assertEqual(result['manual_count'], 2)
        self.assertEqual(result['limit_count'], 1)
        self.assertEqual([job['category'] for job in result['jobs']], ['manual', 'manual', 'limit'])
        self.assertEqual(history['0']['status'], 'skipped_unanswered')
        self.assertFalse(apply.can_retry('skipped_unanswered'))
        self.assertFalse(apply.can_retry('skipped_too_many_steps'))
        self.assertTrue(apply.can_retry('skipped_external_limit'))

    def test_legacy_deferred_form_is_authorized_only_for_selected_manual_session(self):
        token = deferred_review.CURRENT.set(deferred_review.Session('123', True))
        self.addCleanup(deferred_review.CURRENT.reset, token)
        self.assertTrue(deferred_review.authorized('123', {'status': 'skipped_unanswered'}))
        self.assertFalse(deferred_review.authorized('456', {'status': 'skipped_unanswered'}))
        self.assertFalse(deferred_review.authorized('123', {'status': 'submission_unconfirmed'}))


@unittest.skipUnless(os.getenv('AUTOAPPLY_BROWSER_TESTS') == '1', 'offline LinkedIn launch fixtures')
class BrowserLinkedinReadinessTests(SafetyTest):
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
        self.html = ''
        self.ctx.route('**/*', lambda r: r.fulfill(content_type='text/html', body=self.html))
        self.page = self.ctx.new_page()
        self.page.set_default_timeout(500)
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.resume = self.home / 'reviewed.pdf'
        self.resume.write_bytes(b'fixture reviewed CV')
        apply.PROFILE['resume_file'] = str(self.resume)
        apply.SETTINGS['cv_hash'] = hashlib.sha256(self.resume.read_bytes()).hexdigest()
        self.manual = self.enterContext(patch.object(apply, 'record_manual'))

    def load(self, html):
        self.html = html
        self.page.goto('https://www.linkedin.com/jobs/view/123/')
        return self.page.locator('form')

    def test_hidden_follow_is_cleared_through_its_label_without_answering_other_consent(self):
        form = self.load('''<form><input type="checkbox" id="follow-company-checkbox" checked style="display:none">
            <label for="follow-company-checkbox">Follow Example to stay up to date with their page</label>
            <input type="checkbox" id="terms"><label for="terms">I agree to legal terms</label></form>''')
        issues = apply.fill_step(self.page, form)
        self.assertFalse(form.locator('#follow-company-checkbox').is_checked())
        self.assertFalse(form.locator('#terms').is_checked())
        self.assertIn('I agree to legal terms', issues)
        self.assertNotIn('company follow preference was not cleared', apply.unconfirmed_checkbox_questions(form))

    def test_hidden_wrapped_follow_without_an_id_is_also_cleared(self):
        form = self.load('''<form><label><input type="checkbox" checked style="display:none">Follow Example to stay up to date with their page</label></form>''')
        self.assertEqual(apply.fill_step(self.page, form), [])
        self.assertFalse(form.locator('input').is_checked())

    def test_required_follow_is_not_treated_as_optional_preference(self):
        form = self.load('''<form><input type="checkbox" id="follow-company-checkbox" checked required>
            <label for="follow-company-checkbox">Follow Example to stay up to date with their page</label></form>''')
        issues = apply.fill_step(self.page, form)
        self.assertTrue(issues)
        self.assertTrue(form.locator('input').is_checked())

    def upload_fixture(self, selected=True):
        # Like SDUI: no file input inside the form until the browser chooser is used.
        return '''<input type="file" id="chooser" style="display:none" onchange="uploaded()">
            <form><h2>Resume*</h2><button type="button" onclick="document.getElementById('chooser').click()">Upload resume</button><div id="result"></div></form>
            <script>function uploaded(){window.uploads=(window.uploads||0)+1;
            document.getElementById('result').innerHTML='<fieldset role="radiogroup"><div componentkey="easyApplyUploadedResumeRef"><div role="radio" aria-label="reviewed.pdf" aria-checked="''' + str(selected).lower() + '''"><input type="radio" ''' + ('checked' if selected else '') + '''><span>reviewed.pdf</span></div></div></fieldset>';}</script>'''

    def test_lazy_resume_upload_selects_actual_local_cv_and_is_not_a_screening_question(self):
        form = self.load(self.upload_fixture())
        self.assertEqual(apply.fill_step(self.page, form), [])
        self.assertEqual(self.page.evaluate('window.uploads'), 1)
        self.assertEqual(apply.unconfirmed_radio_questions(form), [])
        self.assertFalse(apply.require_manual_review(self.page, form, [], '123', 'Engineer', 'Example'))
        self.assertEqual(apply.fill_step(self.page, form), [])
        self.assertEqual(self.page.evaluate('window.uploads'), 1)

    def test_consecutive_forms_switching_from_aria_wrapper_to_native_radio(self):
        wrapped = self.upload_fixture()
        native = wrapped.replace('<div role="radio" aria-label="reviewed.pdf" aria-checked="true"><input type="radio" checked><span>reviewed.pdf</span></div>',
                                 '<input type="radio" aria-label="reviewed.pdf" checked>')
        self.assertNotEqual(native, wrapped)
        for fixture in [wrapped, native, native]:
            form = self.load(fixture)
            self.assertEqual(apply.fill_step(self.page, form), [])
            self.assertEqual(apply.unconfirmed_radio_questions(form), [])
            self.assertEqual(apply.fill_step(self.page, form), [])
            self.assertEqual(self.page.evaluate('window.uploads'), 1)

    def test_conflicting_native_and_aria_resume_state_is_not_accepted(self):
        form = self.load('''<form><div componentkey="easyApplyUploadedResumeRef"><div role="radio" aria-label="reviewed.pdf" aria-checked="false">
            <input type="radio" checked></div></div></form>''')
        self.assertFalse(linkedin_forms.selected(form.locator(linkedin_forms.RESUME_ROOT), 'reviewed.pdf'))
        form.locator('[role=radio]').evaluate("e=>{e.setAttribute('aria-checked','true');e.setAttribute('aria-label','different.pdf')}")
        self.assertFalse(linkedin_forms.selected(form.locator(linkedin_forms.RESUME_ROOT), 'reviewed.pdf'))

    def test_technical_resume_failure_is_visible_in_error_metrics(self):
        form = self.load(self.upload_fixture())
        with patch.object(self.page, 'expect_file_chooser', side_effect=RuntimeError('fixture failure')), apply.METRICS.application('fixture'):
            issues = apply.fill_step(self.page, form)
        self.assertTrue(any('waiting for the upload file chooser' in issue for issue in issues))
        self.assertEqual(apply.METRICS.snapshot()['errors'], 1)

    def test_unconfirmed_resume_selection_cannot_pass_merely_because_filename_matches(self):
        form = self.load('''<form><fieldset role="radiogroup"><div componentkey="easyApplyUploadedResumeRef">
            <div role="radio" aria-label="reviewed.pdf" aria-checked="true"><input type="radio" checked><span>reviewed.pdf</span></div>
            </div></fieldset></form>''')
        self.assertIn('Resume selection is not confirmed', apply.unconfirmed_radio_questions(form))

    def test_cv_changed_after_preflight_is_not_uploaded(self):
        form = self.load(self.upload_fixture())
        self.resume.write_bytes(b'different CV')
        self.assertIn('CV changed; review it before uploading', apply.fill_step(self.page, form))
        self.assertIsNone(self.page.evaluate('window.uploads'))

    def test_resume_verification_does_not_exempt_an_unrelated_radio_question(self):
        form = self.load(self.upload_fixture())
        self.assertEqual(apply.fill_step(self.page, form), [])
        self.page.locator('fieldset').evaluate("e=>{const other=document.createElement('input');other.type='radio';other.name='screening';e.append(other)}")
        self.assertIn('Resume selection is not confirmed', apply.unconfirmed_radio_questions(form))

    def test_stalled_next_is_recorded_with_reason_and_deferred_instead_of_silent_retry(self):
        self.load('''<button type="button" onclick="document.querySelector('dialog').showModal()">Easy Apply</button>
            <dialog><form><p>Current step</p><footer><button type="button">Next</button></footer></form></dialog>''')
        with patch.object(apply, 'DRY_RUN', True):
            result = apply.apply_to_job(self.page, '123', 'Engineer', 'Example')
        self.assertEqual(result, 'manual_review')
        self.assertIn('did not advance after Next', self.manual.call_args.args[-1])

    def test_only_matching_job_continue_draft_can_open(self):
        self.load('''<a componentkey="jdp-continue-button-123" href="https://www.linkedin.com/jobs/view/123/" onclick="event.preventDefault();document.querySelector('dialog').showModal()">Continue</a>
            <dialog><form><footer><button type="button">Submit application</button></footer></form></dialog>''')
        with patch.object(apply, 'DRY_RUN', True):
            self.assertEqual(apply.apply_to_job(self.page, '123', 'Engineer', 'Example'), 'dry_run')

    def test_unrelated_continue_link_is_not_activated(self):
        for href in ['https://www.linkedin.com/jobs/view/456/', 'https://evil.example/jobs/view/123/']:
            self.load(f'<a componentkey="jdp-continue-button-123" href="{href}" onclick="window.clicked=true;event.preventDefault()">Continue</a>')
            self.assertEqual(apply.apply_to_job(self.page, '123'), 'no_easy_apply')
            self.assertIsNone(self.page.evaluate('window.clicked'))

    def test_disabled_final_submit_is_not_counted_as_a_ready_preview(self):
        self.load('''<button type="button" onclick="document.querySelector('dialog').showModal()">Easy Apply</button>
            <dialog><form><footer><button type="button" disabled>Submit application</button></footer></form></dialog>''')
        with patch.object(apply, 'DRY_RUN', True):
            self.assertEqual(apply.apply_to_job(self.page, '123', 'Engineer', 'Example'), 'manual_review')
        self.assertEqual(apply.METRICS.snapshot()['previews'], 0)
        self.assertIn('submit button is disabled', self.manual.call_args.args[-1])
