"""Reviewed contacts, exact presets and safe ATS reinspection."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import apply
import application_answers as answers
from test_answer_safety import SafetyTest


class ReviewedAnswerTests(SafetyTest):
    def setUp(self):
        super().setUp()
        apply.PROFILE.update(first_name='Test', last_name='User', email='test@example.test',
                             phone='5551234567', phone_intl='+90 5551234567', country_code='(+90)')

    def test_contact_labels_resolve_on_linkedin_not_just_external_forms(self):
        for label, expected in [('First name*', 'Test'), ('Last name*', 'User'),
                                ('Email address*', 'test@example.test'), ('Phone*', '5551234567'),
                                ('Mobile phone number*', '5551234567'), ('Phone country code*', '(+90)')]:
            self.assertEqual(apply.answer(label), [expected])
        self.assertEqual(apply.choose_option('Email*', ['Choose an option', 'test@example.test']), 1)
        self.assertEqual(apply.choose_option('Phone country code*', ['Argentina (+54)', 'Turkey (+90)']), 1)

    def test_calling_code_shared_by_multiple_countries_remains_ambiguous(self):
        apply.PROFILE['country_code'] = '(+1)'
        self.assertIsNone(apply.choose_option('Phone country code*', ['Canada (+1)', 'United States (+1)']))

    def test_formatting_is_not_a_phone_mismatch_but_different_digits_are(self):
        self.assertTrue(apply.text_value_confirmed('(555) 123-4567', ['5551234567'], question='Phone*'))
        self.assertFalse(apply.text_value_confirmed('5551234568', ['5551234567'], question='Phone*'))
        self.assertFalse(apply.text_value_confirmed('+90 5551234567', ['5551234567'], question='Phone*'))
        self.assertTrue(apply.text_value_confirmed('Test@Example.test', ['test@example.test'], question='Email*'))
        self.assertFalse(apply.text_value_confirmed('123-45', ['12345'], question='Salary'))

    def presets(self):
        apply.SETTINGS.update(cv_hash='cv1', question_presets_cv_hash='cv1', question_presets=[
            {'question': 'Will you require visa sponsorship?', 'answer': 'No'},
            {'question': 'Are you ok to be available for periodic on-call shifts?', 'answer': 'Yes'},
        ])

    def test_presets_match_only_reviewed_wording_with_no_sensitive_wildcards(self):
        self.presets()
        self.assertEqual(apply.answer('Will you require visa sponsorship?*'), ['No'])
        self.assertEqual(apply.answer('  WILL you require visa sponsorship?  '), ['No'])
        self.assertEqual(apply.answer('Will you require visa sponsorship in Brazil?'), [])
        self.assertEqual(apply.answer('Do you have authorization to live and work in your country?'), [])
        self.assertEqual(apply.choose_option('Are you ok to be available for periodic on-call shifts?*', ['No', 'Yes']), 1)

    def test_empty_and_old_cv_presets_never_become_yes(self):
        self.presets()
        apply.SETTINGS['question_presets'][0]['answer'] = ''
        self.assertEqual(apply.answer('Will you require visa sponsorship?'), [])
        apply.SETTINGS['cv_hash'] = 'cv2'
        self.assertEqual(apply.answer('Are you ok to be available for periodic on-call shifts?'), [])

    def test_validation_rejects_duplicates_types_and_excess_entries(self):
        for entries in [None, {}, [{'question': 'Q', 'answer': True}],
                        [{'question': 'Q*', 'answer': ''}, {'question': 'q', 'answer': ''}],
                        [{'question': str(i), 'answer': ''} for i in range(11)]]:
            with self.assertRaises(ValueError):
                answers.validate_presets(entries)
        self.assertEqual(len(answers.TEMPLATES), 10)


@unittest.skipUnless(os.getenv('AUTOAPPLY_BROWSER_TESTS') == '1', 'offline browser fixtures')
class BrowserReviewedAnswerTests(SafetyTest):
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
        self.page.set_default_timeout(250)
        apply.PROFILE.update(first_name='Test', last_name='User', email='test@example.test',
                             phone='5551234567', country_code='(+90)', resume_file='nonexistent-resume.pdf')
        self.enterContext(patch.object(apply, 'record_manual'))

    def test_real_controls_fill_and_verify_contact_values(self):
        self.html = '''<div role="dialog"><form>
            <label for="first">First name*</label><input id="first">
            <label for="last">Last name*</label><input id="last">
            <label for="phone">Mobile phone number*</label><input id="phone" value="(555) 123-4567">
            <label for="email">Email address*</label><select id="email"><option>Choose an option</option><option>test@example.test</option></select>
            <label for="code">Phone country code*</label><select id="code"><option>Choose an option</option><option>Argentina (+54)</option><option>Turkey (+90)</option></select>
            </form></div>'''
        self.page.goto('https://www.linkedin.com/jobs/view/123/')
        form = self.page.locator('form')
        self.assertEqual(apply.fill_step(self.page, form), [])
        self.assertFalse(apply.require_manual_review(self.page, form, [], '123', 'Engineer', 'Example'))
        self.assertEqual(form.locator('#first').input_value(), 'Test')
        self.assertEqual(form.locator('#email').input_value(), 'test@example.test')
        self.assertEqual(form.locator('#code').input_value(), 'Turkey (+90)')

    def test_changed_contact_prefill_is_not_silently_overwritten(self):
        self.html = '<form><label for="phone">Phone*</label><input id="phone" value="9999999999"></form>'
        self.page.goto('https://www.linkedin.com/jobs/view/123/')
        self.assertIn('Phone*', apply.fill_step(self.page, self.page.locator('form')))
        self.assertEqual(self.page.locator('#phone').input_value(), '9999999999')

    def test_explicit_contact_replacement_changes_only_recognized_contacts(self):
        apply.SETTINGS['replace_prefilled_contacts'] = True
        apply.CFG['confirmed_answers'] = {'Reference number': '123'}
        self.html = '''<form><label for="phone">Phone*</label><input id="phone" value="9999999999">
            <label for="reference">Reference number</label><input id="reference" value="999"></form>'''
        self.page.goto('https://www.linkedin.com/jobs/view/123/')
        unanswered = apply.fill_step(self.page, self.page.locator('form'))
        self.assertEqual(self.page.locator('#phone').input_value(), '5551234567')
        self.assertEqual(self.page.locator('#reference').input_value(), '999')
        self.assertIn('Reference number', unanswered)

    def test_external_reordered_fields_cannot_receive_each_others_values(self):
        self.html = '''<form><label for="first">First name</label><input id="first" oninput="document.getElementById('last').remove()">
            <label for="last">Last name</label><input id="last"><label for="email">Email</label><input id="email"></form>'''
        self.page.goto('https://job-boards.greenhouse.io/example/jobs/123')
        document = apply.trusted_ats_document(self.page, 'greenhouse')
        with self.assertRaises(apply.ApplicationFormChanged):
            apply.fill_step(self.page, document.locator('form'), external=True)
        self.assertEqual(self.page.locator('#email').input_value(), '')

    def test_document_change_stops_without_filling_new_page(self):
        self.html = '<form><label for="email">Email</label><input id="email"></form>'
        self.page.goto('https://job-boards.greenhouse.io/example/jobs/123')
        document = apply.trusted_ats_document(self.page, 'greenhouse')
        self.page.reload()
        with self.assertRaises(apply.ApplicationFormChanged):
            apply.fill_step(self.page, document.locator('form'), external=True)
        self.assertEqual(self.page.locator('#email').input_value(), '')

    def test_same_document_rerender_is_reinspected_once_and_preview_never_submits(self):
        self.html = '''<form onsubmit="window.submitted=true;return false">
            <label for="first">First name</label><input id="first" oninput="document.getElementById('last')?.remove()">
            <label for="last">Last name</label><input id="last">
            <label for="email">Email</label><input id="email" type="email">
            <button type="submit">Submit</button></form>'''
        url = 'https://job-boards.greenhouse.io/example/jobs/123'
        self.page.goto(url)
        with patch.object(apply, 'DRY_RUN', True):
            self.assertEqual(apply.apply_trusted_form(self.page, '123', 'Engineer', 'Example', url, 'greenhouse'), 'dry_run')
        self.assertEqual(self.page.locator('#email').input_value(), 'test@example.test')
        self.assertFalse(self.page.evaluate('window.submitted === true'))

    def test_select_changed_after_fill_is_blocked_before_preview(self):
        self.html = '''<form><label for="email">Email address*</label>
            <select id="email"><option>Choose an option</option><option>test@example.test</option></select></form>'''
        self.page.goto('https://www.linkedin.com/jobs/view/123/')
        form = self.page.locator('form')
        self.assertEqual(apply.fill_step(self.page, form), [])
        self.page.locator('#email').select_option(index=0)
        self.assertTrue(apply.require_manual_review(self.page, form, [], '123', 'Engineer', 'Example'))

    def test_aria_hidden_react_required_proxy_is_not_filled_or_ignored_by_validation(self):
        self.html = '''<form><label for="first">First name</label><input id="first">
            <input required aria-hidden="true" tabindex="-1" style="opacity:0"></form>'''
        self.page.goto('https://job-boards.greenhouse.io/example/jobs/123')
        document = apply.trusted_ats_document(self.page, 'greenhouse')
        form = document.locator('form')
        self.assertEqual(apply.fill_step(self.page, form, external=True), [])
        self.assertEqual(form.locator('[aria-hidden=true]').input_value(), '')
        self.assertTrue(apply.require_manual_review(self.page, form, [], '123', 'Engineer', 'Example', external=True))

    def test_resume_upload_removing_input_does_not_shift_cover_letter_locator(self):
        home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        resume = home / 'resume.pdf'
        resume.write_bytes(b'fixture resume')
        apply.PROFILE['resume_file'] = str(resume)
        self.html = '''<form><label for="resume">Resume</label><input id="resume" type="file" onchange="this.remove()">
            <label for="cover">Cover letter</label><input id="cover" type="file"></form>'''
        self.page.goto('https://job-boards.greenhouse.io/example/jobs/123')
        document = apply.trusted_ats_document(self.page, 'greenhouse')
        self.assertEqual(apply.fill_step(self.page, document.locator('form'), external=True), [])
        self.assertEqual(self.page.locator('#cover').evaluate('e => e.files.length'), 0)

    def test_greenhouse_phone_country_and_react_selected_value_are_verified(self):
        self.html = '''<form><label for="country">Country*</label>
            <div class="select__control"><span class="select__single-value" id="selected"></span>
            <input id="country" role="combobox" onclick="document.getElementById('options').style.display='block'"></div>
            <div id="options" role="listbox" style="display:none"><div role="option" onclick="document.getElementById('selected').textContent=this.textContent;document.getElementById('country').value='';this.parentElement.style.display='none'">Turkey+90</div></div>
            <label for="phone">Phone*</label><input id="phone" type="tel"></form>'''
        self.page.goto('https://job-boards.greenhouse.io/example/jobs/123')
        document = apply.trusted_ats_document(self.page, 'greenhouse')
        form = document.locator('form')
        self.assertEqual(apply.fill_step(self.page, form, external=True), [])
        self.assertEqual(self.page.locator('#phone').input_value(), '5551234567')
        self.assertEqual(self.page.locator('#country').input_value(), '')
        self.assertFalse(apply.require_manual_review(self.page, form, [], '123', 'Engineer', 'Example', external=True))
