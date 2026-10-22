"""Follow-up review #1: nonempty text is not proof of a confirmed answer."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_answer_safety import SafetyTest
import apply
import sources


class TextValueConfirmationTests(SafetyTest):
    def test_exact_value_and_line_endings_are_confirmed(self):
        self.assertTrue(apply.text_value_confirmed(' No ', ['No']))
        self.assertTrue(apply.text_value_confirmed('First\r\nSecond', ['First\nSecond']))

    def test_empty_unknown_and_different_values_are_not_confirmed(self):
        for value, candidates in [('', ['']), (' ', ['No']), ('Yes', ['No']), ('Site supplied', []),
                                  ('A different fact', ['A fact']), ('user@wrong.test', ['user@right.test'])]:
            self.assertFalse(apply.text_value_confirmed(value, candidates))

    def test_numeric_equivalence_does_not_guess_from_partial_numbers(self):
        self.assertTrue(apply.text_value_confirmed('60000.00', ['60000'], numeric=True))
        self.assertTrue(apply.text_value_confirmed('0', ['0.00'], numeric=True))
        for value in ['1', '60000 USD', 'NaN', 'Infinity', '-60000']:
            self.assertFalse(apply.text_value_confirmed(value, ['60000'], numeric=True))

    def test_exact_answer_overrides_standard_profile_field(self):
        from test_answer_safety import node
        field = node()
        field.get_attribute.return_value = ''
        apply.PROFILE['email'] = 'profile@example.test'
        apply.CFG['confirmed_answers'] = {'Email': 'explicit@example.test'}
        self.assertEqual(apply.text_field_candidates(field, 'Email', external=True), ['explicit@example.test'])


@unittest.skipUnless(os.getenv('AUTOAPPLY_BROWSER_TESTS') == '1', 'optional local Chrome prefill tests')
class BrowserPrefilledAnswerTests(SafetyTest):
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
        self.context.route('**/*', lambda route: route.abort())
        self.page = self.context.new_page()
        self.page.set_default_timeout(1000)
        self.manual = self.enterContext(patch.object(apply, 'record_manual'))
        self.record = self.enterContext(patch.object(apply, 'record'))
        self.question = 'Will you require visa sponsorship?'
        apply.CFG['confirmed_answers'] = {self.question: 'No'}

    def form(self, fields, modal=False):
        self.page.set_content('<script>window.sent=0;window.changed=0</script>'
            + ('<button type="button">Easy Apply</button><div role="dialog">' if modal else '')
            + '<form onsubmit="event.preventDefault();window.sent++">' + fields
            + '<footer><button type="submit">Submit application</button></footer></form>'
            + ('</div>' if modal else ''))
        return self.page.locator('form')

    def review(self, form, issues=None, external=False):
        return apply.require_manual_review(self.page, form, issues or [], 'fixture', 'Engineer', 'Example', external=external)

    def test_opposite_prefilled_legal_answer_is_not_trusted_or_overwritten(self):
        form = self.form(f'<label for="visa">{self.question}</label><input id="visa" value="Yes" required>')
        issues = apply.fill_step(self.page, form)
        self.assertIn(self.question, issues)
        self.assertEqual(self.page.locator('#visa').input_value(), 'Yes')
        self.assertTrue(self.review(form, issues))

    def test_wrong_prefilled_salary_is_blocked(self):
        form = self.form('<label for="salary">Annual salary in USD</label><input id="salary" type="number" value="1" required>')
        self.assertIn('Annual salary in USD', apply.fill_step(self.page, form))
        self.assertEqual(self.page.locator('#salary').input_value(), '1')
        self.assertTrue(self.review(form))

    def test_unknown_optional_prefill_is_not_a_confirmed_answer(self):
        form = self.form('<label for="project">Explain your hardest project</label><textarea id="project">A site supplied story</textarea>')
        self.assertTrue(apply.fill_step(self.page, form))
        self.assertTrue(self.review(form))

    def test_correct_prefill_does_not_fire_input_again(self):
        form = self.form(f'<label for="visa">{self.question}</label><input id="visa" value="No" oninput="window.changed++">')
        self.assertEqual(apply.fill_step(self.page, form), [])
        self.assertFalse(self.review(form))
        self.assertEqual(self.page.evaluate('window.changed'), 0)

    def test_equivalent_numeric_prefill_is_accepted(self):
        form = self.form('<label for="salary">Annual salary in USD</label><input id="salary" type="number" step="any" value="60000.00">')
        self.assertEqual(apply.fill_step(self.page, form), [])
        self.assertFalse(self.review(form))

    def test_readonly_wrong_answer_blocks_but_disabled_field_is_ignored(self):
        form = self.form(f'<label for="visa">{self.question}</label><input id="visa" value="Yes" readonly>')
        self.assertTrue(self.review(form))
        form = self.form(f'<label for="visa">{self.question}</label><input id="visa" value="Yes" disabled>')
        self.assertEqual(apply.fill_step(self.page, form), [])
        self.assertFalse(self.review(form))

    def test_correct_empty_field_fill_is_verified(self):
        form = self.form(f'<label for="visa">{self.question}</label><input id="visa" required>')
        self.assertEqual(apply.fill_step(self.page, form), [])
        self.assertEqual(self.page.locator('#visa').input_value(), 'No')
        self.assertFalse(self.review(form))

    def test_site_reverts_newly_filled_answer(self):
        form = self.form(f'''<label for="visa">{self.question}</label><input id="visa" oninput="this.value='Yes'">''')
        self.assertTrue(apply.fill_step(self.page, form))
        self.assertTrue(self.review(form))

    def test_site_resets_answer_after_fill_before_final_review(self):
        form = self.form(f'<label for="visa">{self.question}</label><input id="visa">')
        self.assertEqual(apply.fill_step(self.page, form), [])
        self.page.locator('#visa').evaluate("e => e.value = 'Yes'")
        self.assertTrue(self.review(form))

    def test_standard_external_email_is_checked_against_profile(self):
        apply.PROFILE['email'] = 'correct@example.test'
        form = self.form('<label for="email">Email</label><input id="email" type="email" value="wrong@example.test">')
        self.assertTrue(apply.fill_step(self.page, form, external=True))
        self.assertTrue(self.review(form, external=True))
        self.page.locator('#email').fill('correct@example.test')
        self.assertEqual(apply.fill_step(self.page, form, external=True), [])
        self.assertFalse(self.review(form, external=True))

    def test_resume_upload_cannot_reset_verified_answer_silently(self):
        home = Path(self.enterContext(tempfile.TemporaryDirectory(prefix='prefill-resume-')))
        resume = home / 'resume.pdf'
        resume.write_bytes(b'local fixture')
        apply.PROFILE['resume_file'] = str(resume)
        form = self.form(f'''<label for="visa">{self.question}</label><input id="visa">
            <label for="resume">Resume</label><input id="resume" type="file" onchange="document.getElementById('visa').value='Yes'">''')
        issues = apply.fill_step(self.page, form, external=True)
        self.assertTrue(self.review(form, issues, external=True))

    def test_wrong_prefill_blocks_linkedin_submission_in_both_modes(self):
        for mode in [True, False]:
            with self.subTest(mode=mode), patch.object(apply, 'DRY_RUN', mode):
                self.form(f'<label for="visa">{self.question}</label><input id="visa" value="Yes">', modal=True)
                self.assertEqual(apply.apply_to_job(self.page, 'fixture', 'Engineer', 'Example'), 'manual_review')
                self.assertEqual(self.page.evaluate('window.sent'), 0)
        self.record.assert_not_called()

    def test_wrong_prefill_blocks_ats_submission_in_both_modes(self):
        self.form('<label for="email">Email</label><input id="email" type="email" value="wrong@example.test">')
        apply.PROFILE['email'] = 'correct@example.test'
        html = self.page.content()
        self.context.route('https://jobs.lever.co/**', lambda route: route.fulfill(content_type='text/html', body=html))
        with patch.object(apply, 'EXTERNAL', True), patch.object(apply, 'EXT_STATE', {'count': 0, 'last': 0.0}):
            for mode in [True, False]:
                with self.subTest(mode=mode), patch.object(apply, 'DRY_RUN', mode):
                    result = apply.apply_form(self.page, 'fixture', 'Engineer', 'Example', 'https://jobs.lever.co/example/job/apply')
                    self.assertEqual(result, 'manual_review')
                    self.assertEqual(self.page.evaluate('window.sent'), 0)
        self.record.assert_not_called()

    def test_wrong_prefill_blocks_indeed_and_wellfound_in_both_modes(self):
        self.form(f'<label for="visa">{self.question}</label><input id="visa" value="Yes">', modal=True)
        html = self.page.content().replace('Easy Apply', 'Apply now').replace('<button type="button">', '<button id="indeedApplyButton" type="button">', 1)
        self.context.route('https://wellfound.com/**', lambda route: route.fulfill(content_type='text/html', body=html))
        self.context.route('https://www.indeed.com/**', lambda route: route.fulfill(content_type='text/html', body=html))
        with patch.object(sources, 'core', apply), patch.object(sources, 'goto', return_value=True), \
                patch.object(sources, 'wellfound_login', return_value=True), patch.object(apply, 'wait_human', return_value=True):
            for site, submitter in [('https://wellfound.com/jobs/fixture', sources.wellfound_apply),
                                    ('https://www.indeed.com/viewjob?jk=fixture', sources.indeed_apply)]:
                for mode in [True, False]:
                    with self.subTest(site=site, mode=mode), patch.object(apply, 'DRY_RUN', mode):
                        self.page.goto(site)
                        job = {'id': 'fixture', 'title': 'Engineer', 'company': 'Example', 'url': site}
                        self.assertEqual(submitter(self.page, job), 'manual_review')
                        self.assertEqual(self.page.evaluate('window.sent'), 0)
        self.record.assert_not_called()
