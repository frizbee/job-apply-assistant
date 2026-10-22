"""Follow-up review #2: checkbox answers must be complete and stay selected."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_answer_safety import SafetyTest
import apply
import sources


class CheckboxChoiceTests(SafetyTest):
    question = 'Which certifications do you hold?'

    def test_all_confirmed_choices_must_exist(self):
        apply.CFG['confirmed_answers'] = {self.question: ['A', 'B']}
        self.assertEqual(apply.checkbox_choices(self.question, ['A', 'B', 'C']), [0, 1])
        self.assertIsNone(apply.checkbox_choices(self.question, ['A', 'C']))

    def test_duplicate_option_or_repeated_target_is_ambiguous(self):
        apply.CFG['confirmed_answers'] = {self.question: ['A']}
        self.assertIsNone(apply.checkbox_choices(self.question, ['A', 'A']))
        apply.CFG['confirmed_answers'] = {self.question: ['A', 'A']}
        self.assertIsNone(apply.checkbox_choices(self.question, ['A', 'B']))

    def test_unknown_or_blank_question_does_not_choose(self):
        self.assertIsNone(apply.checkbox_choices(self.question, ['A', 'B']))
        apply.CFG['confirmed_answers'] = {'': ['A']}
        self.assertIsNone(apply.checkbox_choices('', ['A', 'B']))

    def test_exact_boolean_set_is_required(self):
        state = {'question': self.question, 'options': ['A', 'B'], 'states': [True, False], 'disabled': [False, False]}
        self.assertTrue(apply.checkbox_group_confirmed(state, [0]))
        for states in [[False, False], [True, True], [None, False], [1, 0], [True]]:
            self.assertFalse(apply.checkbox_group_confirmed({**state, 'states': states}, [0]))

    def test_disabled_required_choice_is_not_confirmed(self):
        state = {'options': ['A', 'B'], 'states': [True, False], 'disabled': [True, False]}
        self.assertFalse(apply.checkbox_group_confirmed(state, [0]))


@unittest.skipUnless(os.getenv('AUTOAPPLY_BROWSER_TESTS') == '1', 'optional local Chrome checkbox tests')
class BrowserCheckboxSelectionTests(SafetyTest):
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
        self.question = 'Which certifications do you hold?'
        apply.CFG['confirmed_answers'] = {self.question: ['A', 'B']}
        self.manual = self.enterContext(patch.object(apply, 'record_manual'))
        self.record = self.enterContext(patch.object(apply, 'record'))

    def options(self, second='B', first_attributes='', second_attributes=''):
        return (f'<fieldset><legend>{self.question}</legend>'
                f'<label id="label-a"><input id="a" type="checkbox" {first_attributes}>A</label>'
                f'<label id="label-b"><input id="b" type="checkbox" {second_attributes}>{second}</label></fieldset>')

    def form(self, content=None, modal=False):
        if content is None:
            content = self.options()
        self.page.set_content('<script>window.sent=0;window.changed=0</script>'
            + ('<button type="button">Easy Apply</button><div role="dialog">' if modal else '')
            + '<form onsubmit="event.preventDefault();window.sent++">' + content
            + '<footer><button type="submit">Submit application</button></footer></form>'
            + ('</div>' if modal else ''))
        return self.page.locator('form')

    def review(self, form, issues=None):
        return apply.require_manual_review(self.page, form, issues or [], 'fixture', 'Engineer', 'Example')

    def test_missing_confirmed_option_blocks_without_partial_selection(self):
        form = self.form(self.options(second='C'))
        self.assertEqual(apply.fill_checkgroups(form), [self.question])
        self.assertEqual(form.locator('input:checked').count(), 0)
        self.assertTrue(self.review(form))

    def test_later_choice_resetting_earlier_one_is_not_success(self):
        form = self.form(self.options(second_attributes='''onchange="document.getElementById('a').checked=false"'''))
        self.assertEqual(apply.fill_checkgroups(form), [self.question])
        self.assertFalse(self.page.locator('#a').is_checked())
        self.assertTrue(self.review(form))

    def test_complete_choice_is_confirmed_and_not_clicked_twice(self):
        form = self.form(self.options(first_attributes='onchange="window.changed++"', second_attributes='onchange="window.changed++"'))
        self.assertEqual(apply.fill_checkgroups(form), [])
        self.assertEqual(apply.fill_checkgroups(form), [])
        self.assertFalse(self.review(form))
        self.assertEqual(self.page.evaluate('window.changed'), 2)

    def test_extra_preselected_choice_is_removed(self):
        apply.CFG['confirmed_answers'] = {self.question: ['A']}
        form = self.form(self.options(second_attributes='checked'))
        self.assertEqual(apply.fill_checkgroups(form), [])
        self.assertTrue(self.page.locator('#a').is_checked())
        self.assertFalse(self.page.locator('#b').is_checked())
        self.assertFalse(self.review(form))

    def test_duplicate_label_requires_manual_review(self):
        apply.CFG['confirmed_answers'] = {self.question: ['A']}
        form = self.form(self.options(second='A'))
        self.assertEqual(apply.fill_checkgroups(form), [self.question])
        self.assertEqual(form.locator('input:checked').count(), 0)

    def test_disabled_required_choice_is_unanswered(self):
        form = self.form(self.options(second_attributes='disabled'))
        self.assertTrue(apply.fill_checkgroups(form))
        self.assertTrue(self.review(form))

    def test_indeterminate_state_is_not_confirmation(self):
        form = self.form(self.options(first_attributes='checked', second_attributes='checked'))
        self.page.locator('#a').evaluate('e => e.indeterminate = true')
        self.assertTrue(self.review(form))

    def test_native_and_aria_disagreement_is_not_confirmation(self):
        form = self.form(self.options(first_attributes='checked aria-checked="false"', second_attributes='checked'))
        self.assertTrue(self.review(form))

    def test_option_labels_changing_during_selection_block(self):
        form = self.form(self.options(first_attributes='''onchange="document.getElementById('label-b').lastChild.textContent='C'"'''))
        self.assertTrue(apply.fill_checkgroups(form))
        self.assertFalse(self.page.locator('#b').is_checked())
        self.assertTrue(self.review(form))

    def test_reset_after_filling_is_caught_before_submit(self):
        form = self.form()
        self.assertEqual(apply.fill_checkgroups(form), [])
        self.page.locator('#a').evaluate('e => e.checked = false')
        self.assertTrue(self.review(form))

    def test_extra_checked_after_filling_is_caught(self):
        apply.CFG['confirmed_answers'] = {self.question: ['A']}
        form = self.form()
        self.assertEqual(apply.fill_checkgroups(form), [])
        self.page.locator('#b').evaluate('e => e.checked = true')
        self.assertTrue(self.review(form))

    def test_group_shrinking_to_one_option_cannot_bypass_review(self):
        form = self.form()
        self.assertEqual(apply.fill_checkgroups(form), [])
        self.page.locator('#label-b').evaluate('e => e.remove()')
        self.assertTrue(self.review(form))
        self.assertEqual(form.locator('[data-checkgroup]').count(), 0)

    def test_new_unconfirmed_checked_option_is_caught(self):
        form = self.form()
        self.assertEqual(apply.fill_checkgroups(form), [])
        self.page.locator('fieldset').evaluate('''e => e.insertAdjacentHTML('beforeend', '<label><input type="checkbox" checked>C</label>')''')
        self.assertTrue(self.review(form))

    def test_standalone_consent_reset_cannot_hide_after_other_fields(self):
        question = 'I agree to privacy terms'
        apply.CFG['confirmed_answers'] = {question: 'Yes'}
        form = self.form(f'<label for="consent">{question}</label><input id="consent" type="checkbox">')
        self.assertEqual(apply.fill_step(self.page, form), [])
        self.assertFalse(self.review(form))
        self.page.locator('#consent').evaluate('e => e.checked = false')
        self.assertTrue(self.review(form))

    def test_hidden_native_inputs_can_use_safe_labels(self):
        form = self.form(self.options(first_attributes='style="display:none"', second_attributes='style="display:none"'))
        self.assertEqual(apply.fill_checkgroups(form), [])
        self.assertTrue(self.page.locator('#a').is_checked())
        self.assertTrue(self.page.locator('#b').is_checked())
        self.assertFalse(self.review(form))

    def test_submit_capable_wrapper_cannot_be_clicked_while_selecting(self):
        content = (f'<fieldset><legend>{self.question}</legend><button type="submit">'
                   '<label><input type="checkbox">A</label></button><label><input type="checkbox">B</label></fieldset>')
        form = self.form(content)
        self.assertTrue(apply.fill_checkgroups(form))
        self.assertEqual(self.page.evaluate('window.sent'), 0)

    def test_upload_reset_is_caught_by_final_review(self):
        home = Path(self.enterContext(tempfile.TemporaryDirectory(prefix='checkbox-resume-')))
        resume = home / 'resume.pdf'
        resume.write_bytes(b'fixture')
        apply.PROFILE['resume_file'] = str(resume)
        form = self.form(self.options() + '''<label for="resume">Resume</label>
            <input id="resume" type="file" onchange="document.getElementById('a').checked=false">''')
        issues = apply.fill_step(self.page, form, external=True)
        self.assertTrue(self.review(form, issues))

    def test_partial_group_blocks_linkedin_in_both_modes(self):
        for mode in [True, False]:
            with self.subTest(mode=mode), patch.object(apply, 'DRY_RUN', mode):
                self.form(self.options(second='C'), modal=True)
                self.assertEqual(apply.apply_to_job(self.page, 'fixture', 'Engineer', 'Example'), 'manual_review')
                self.assertEqual(self.page.evaluate('window.sent'), 0)
        self.record.assert_not_called()

    def test_reset_group_blocks_ats_in_both_modes(self):
        self.form(self.options(second_attributes='''onchange="document.getElementById('a').checked=false"'''))
        html = self.page.content()
        self.context.route('https://jobs.lever.co/**', lambda route: route.fulfill(content_type='text/html', body=html))
        with patch.object(apply, 'EXTERNAL', True), patch.object(apply, 'EXT_STATE', {'count': 0, 'last': 0.0}):
            for mode in [True, False]:
                with self.subTest(mode=mode), patch.object(apply, 'DRY_RUN', mode):
                    self.assertEqual(apply.apply_form(self.page, 'fixture', 'Engineer', 'Example', 'https://jobs.lever.co/example/job/apply'), 'manual_review')
                    self.assertEqual(self.page.evaluate('window.sent'), 0)
        self.record.assert_not_called()

    def test_partial_group_blocks_indeed_and_wellfound(self):
        self.form(self.options(second='C'), modal=True)
        html = self.page.content().replace('Easy Apply', 'Apply now').replace('<button type="button">', '<button id="indeedApplyButton" type="button">', 1)
        for domain in ['https://wellfound.com/**', 'https://www.indeed.com/**']:
            self.context.route(domain, lambda route: route.fulfill(content_type='text/html', body=html))
        with patch.object(sources, 'core', apply), patch.object(sources, 'goto', return_value=True), \
                patch.object(sources, 'wellfound_login', return_value=True), patch.object(apply, 'wait_human', return_value=True):
            for site, submitter in [('https://wellfound.com/jobs/fixture', sources.wellfound_apply),
                                    ('https://www.indeed.com/viewjob?jk=fixture', sources.indeed_apply)]:
                for mode in [True, False]:
                    with self.subTest(site=site, mode=mode), patch.object(apply, 'DRY_RUN', mode):
                        self.page.goto(site)
                        self.assertEqual(submitter(self.page, {'id': 'fixture', 'title': 'Engineer', 'company': 'Example', 'url': site}), 'manual_review')
                        self.assertEqual(self.page.evaluate('window.sent'), 0)
        self.record.assert_not_called()
