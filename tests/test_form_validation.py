"""Native constraints and selected React dropdown answers in assisted review."""
import os
import unittest
import threading
from unittest.mock import patch

import apply
from test_answer_safety import SafetyTest
from tools.restore_review_form import restore_snapshot


@unittest.skipUnless(os.getenv('AUTOAPPLY_BROWSER_TESTS') == '1', 'local browser validation fixtures')
class BrowserFormValidationTests(SafetyTest):
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

    def issues(self, html):
        self.page.set_content('<form>' + html + '</form>')
        return apply.native_form_issues(self.page.locator('form'))

    def dropdown(self, *, selected=True, proxy=False, invalid=False, label='Country*', ident='country'):
        value = '<div class="select__single-value">Argentina</div>' if selected else '<div class="select__placeholder">Select...</div>'
        sentinel = '<input required tabindex="-1" aria-hidden="true" style="position:absolute;width:1px;height:1px;opacity:0">' if proxy else ''
        return f'''<label id="{ident}-label" for="{ident}">{label}</label>
            <div class="select-shell"><div class="select__control"><div class="select__value-container">
            {value}<div class="select__input-container"><input id="{ident}" role="combobox"
            aria-labelledby="{ident}-label" aria-required="true" aria-invalid="{str(invalid).lower()}" value=""></div>
            </div></div>{sentinel}</div>'''

    def test_selected_react_value_is_not_the_empty_search_input(self):
        self.assertEqual(self.issues(self.dropdown()), [])
        self.assertEqual(self.page.locator('#country').input_value(), '')

    def test_actual_missing_dropdown_and_native_proxy_share_one_human_message(self):
        issues = self.issues(self.dropdown(selected=False, proxy=True, ident='question_13031517007', label='Do you reside in the United States, Canada, or Mexico?*'))
        self.assertEqual(issues, ['Do you reside in the United States, Canada, or Mexico?* — choose an option from the dropdown list'])
        self.assertNotIn('question_13031517007', str(issues))

    def test_country_city_and_sensitive_questions_are_identified_by_their_labels(self):
        issues = self.issues(''.join(self.dropdown(selected=False, proxy=True, label=label, ident=ident)
                                    for ident, label in [('country', 'Country*'), ('candidate-location', 'Location (City)*'),
                                                         ('question_13031518007', 'Do you require visa sponsorship?*')]))
        self.assertEqual(len(issues), 3)
        self.assertTrue(issues[0].startswith('Country*'))
        self.assertTrue(issues[1].startswith('Location (City)*'))
        self.assertTrue(issues[2].startswith('Do you require visa sponsorship?*'))

    def test_native_required_proxy_is_not_ignored_when_site_has_not_accepted_selection(self):
        issues = self.issues(self.dropdown(proxy=True))
        self.assertEqual(issues, ['Country* — the site has not accepted this selection; choose the option again'])

    def test_explicit_site_error_blocks_even_a_nonempty_selected_option(self):
        self.assertEqual(self.issues(self.dropdown(invalid=True)), ['Country* — the site reports an invalid value'])

    def test_hidden_conditional_fields_do_not_create_phantom_user_questions(self):
        issues = self.issues('''<div hidden><label for="conditional">Conditional</label><input id="conditional" required></div>
            <div style="display:none"><input required></div>
            <div inert><input required></div>
            <label for="actual">Actual answer*</label><input id="actual" required>''')
        self.assertEqual(issues, ['Actual answer* — complete this required field'])

    def test_aria_hidden_alone_does_not_exempt_native_required_constraints(self):
        self.assertEqual(self.issues('<input required aria-hidden="true" tabindex="-1">'),
                         ['Required field — complete this required field'])

    def test_required_file_and_invalid_email_still_block(self):
        issues = self.issues('''<label for="cv">Resume*</label><input id="cv" type="file" required>
            <label for="email">Email*</label><input id="email" type="email" value="not-an-email" required>''')
        self.assertEqual(issues, ['Resume* — complete this required field', 'Email* — the site reports an invalid value'])

    def test_disabled_and_readonly_controls_do_not_block(self):
        self.assertEqual(self.issues('<input required disabled><input required readonly>'), [])

    def test_required_native_radio_group_is_satisfied_by_one_checked_member(self):
        self.assertEqual(self.issues('''<fieldset><legend>Work authorization*</legend>
            <input type="radio" name="authorization" required aria-required="true" checked>
            <input type="radio" name="authorization" required aria-required="true"></fieldset>'''), [])

    def test_greenhouse_required_single_acknowledgement_fieldset_accepts_checked_native_box(self):
        self.assertEqual(self.issues('''<fieldset class="checkbox" aria-required="true">
            <legend>AI guidelines*</legend><label><input type="checkbox" required checked>Acknowledge</label>
            </fieldset>'''), [])
        self.assertTrue(self.page.locator('input[type=checkbox]').is_checked())

    def test_single_required_acknowledgement_is_not_inferred(self):
        self.assertTrue(self.issues('''<fieldset aria-required="true"><legend>Recording policy*</legend>
            <label><input type="checkbox" required>Acknowledge</label></fieldset>'''))
        self.assertFalse(self.page.locator('input[type=checkbox]').is_checked())

    def test_contradictory_or_disabled_single_acknowledgement_stays_manual(self):
        for attrs in ['checked aria-checked="false"', 'checked disabled']:
            with self.subTest(attrs=attrs):
                self.assertTrue(self.issues(f'''<fieldset aria-required="true"><legend>Policy*</legend>
                    <input type="checkbox" required {attrs}></fieldset>'''))

    def test_unbound_internal_id_is_not_presented_as_a_question(self):
        self.assertEqual(self.issues('<input id="question_12345" required>'), ['Required field — complete this required field'])

    def test_required_multiselect_accepts_selected_chips_not_empty_search_text(self):
        html = self.dropdown().replace('<div class="select__single-value">Argentina</div>',
                                       '<div class="select__multi-value__label">Argentina</div><div class="select__multi-value__label">Uruguay</div>')
        self.assertEqual(self.issues(html), [])

    def test_contradictory_selected_widgets_require_review(self):
        html = self.dropdown().replace('<div class="select__single-value">Argentina</div>',
                                       '<div class="select__single-value">Argentina</div><div class="select__single-value">Uruguay</div>')
        self.assertEqual(self.issues(html), ['Country* — the selected option cannot be identified uniquely'])

    def checkbox_group(self, *, selected=('LinkedIn',), required=True, extra=''):
        options = ['LinkedIn', 'Glassdoor', 'Indeed']
        boxes = ''.join(f'<div><input type="checkbox" id="source-{i}" name="source[]" {"required" if required else ""} '
                        f'aria-invalid="false" {"checked" if label in selected else ""}><label for="source-{i}">{label}</label></div>'
                        for i, label in enumerate(options))
        return f'''<fieldset class="checkbox" id="sources" aria-required="{str(required).lower()}" {extra}>
            <legend>How did you hear about this opportunity? (select all that apply) *</legend>{boxes}</fieldset>'''

    def test_required_checkbox_choice_group_needs_one_applicable_option_not_all(self):
        self.assertEqual(self.issues(self.checkbox_group()), [])
        self.assertEqual(self.page.locator('input[type=checkbox]').evaluate_all('es=>es.map(e=>e.checked)'), [True, False, False])
        # The checker must not mutate the site's attributes or select extra answers.
        self.assertEqual(self.page.locator('input[required]').count(), 3)

    def test_empty_required_checkbox_group_reports_only_the_group_question(self):
        issues = self.issues(self.checkbox_group(selected=()))
        self.assertEqual(issues, ['How did you hear about this opportunity? (select all that apply) * — select at least one applicable option'])

    def test_multiple_true_answers_and_empty_optional_checkbox_groups_are_valid(self):
        self.assertEqual(self.issues(self.checkbox_group(selected=('LinkedIn', 'Indeed'))), [])
        self.assertEqual(self.issues(self.checkbox_group(selected=(), required=False)), [])

    def test_checkbox_group_does_not_relax_a_separate_required_consent(self):
        html = self.checkbox_group() + '<label for="consent">I agree to the privacy policy*</label><input id="consent" type="checkbox" required>'
        self.assertEqual(self.issues(html), ['I agree to the privacy policy* — complete this required field'])

    def test_select_all_legal_agreements_remain_individually_required(self):
        html = self.checkbox_group().replace('How did you hear about this opportunity?', 'Select your legal agreements')
        self.assertTrue(self.issues(html))

    def test_checkbox_group_preserves_site_errors_and_ambiguous_states(self):
        self.assertEqual(self.issues(self.checkbox_group(extra='aria-invalid="true"')),
                         ['How did you hear about this opportunity? (select all that apply) * — the site reports an invalid value'])
        self.issues(self.checkbox_group())
        self.page.locator('#source-0').evaluate("e=>e.setAttribute('aria-checked','false')")
        self.assertEqual(apply.native_form_issues(self.page.locator('form')),
                         ['How did you hear about this opportunity? (select all that apply) * — the selected options cannot be verified'])

    def test_disabled_checked_choice_cannot_satisfy_the_required_group(self):
        self.issues(self.checkbox_group())
        self.page.locator('#source-0').evaluate('e=>e.disabled=true')
        self.assertEqual(apply.native_form_issues(self.page.locator('form')),
                         ['How did you hear about this opportunity? (select all that apply) * — select at least one applicable option'])

    def uploaded_resume(self):
        return '''<div class="file-upload" role="group" aria-required="true" aria-labelledby="resume-label">
            <div id="resume-label">Resume/CV*</div><div class="file-upload__wrapper"><div class="file-upload__filename">
            <p>reviewed.pdf</p><button type="button" aria-label="Remove file">Remove</button></div></div></div>'''

    def test_greenhouse_completed_upload_satisfies_required_group_without_file_input(self):
        self.assertEqual(self.issues(self.uploaded_resume()), [])

    def test_upload_filename_without_receipt_control_or_site_acceptance_is_not_enough(self):
        for html in [self.uploaded_resume().replace('aria-label="Remove file"', ''),
                     self.uploaded_resume().replace('aria-label="Remove file"', 'aria-label="Remove file" disabled'),
                     self.uploaded_resume().replace('aria-required="true"', 'aria-required="true" aria-invalid="true"'),
                     self.uploaded_resume().replace('<p>reviewed.pdf</p>', '<p></p>')]:
            with self.subTest(html=html):
                self.assertTrue(self.issues(html))

    def test_greenhouse_live_fleetio_widget_shapes_have_no_phantom_errors(self):
        html = self.dropdown(label='Country*', ident='country')
        html += self.dropdown(label='Location (City)*', ident='candidate-location')
        html += self.dropdown(label='Do you reside in the United States, Canada, or Mexico?*', ident='question_13031517007')
        html += self.dropdown(label='Do you require visa sponsorship?*', ident='question_13031518007')
        html += self.uploaded_resume()
        self.assertEqual(self.issues(html), [])

    def recovery_fixture(self):
        url = 'https://job-boards.greenhouse.io/example/jobs/123'
        html = '''<form><label for="answer">Why this role?*</label><textarea id="answer"></textarea>
            <label for="choice">Reviewed choice*</label><div class="select-shell"><div class="select__control">
            <div class="select__value-container"><input id="choice" type="text" role="combobox" aria-required="true"
            aria-controls="choices" oninput="options()"></div></div></div><div id="choices"></div>
            <button type="submit" onclick="window.sent=true">Submit</button></form>
            <script>function options(){const root=document.getElementById('choices');root.innerHTML='';
                for(const name of ['Yes','No']){const option=document.createElement('div');option.setAttribute('role','option');
                    option.textContent=name;option.onclick=()=>{document.getElementById('choice').value='';
                        const value=document.createElement('div');value.className='select__single-value';value.textContent=name;
                        document.querySelector('.select__value-container').append(value);root.innerHTML=''};root.append(option)}}
            </script>'''
        self.context.route('**/*', lambda route: route.fulfill(content_type='text/html', body=html))
        self.page.goto(url)
        snapshot = {'url': url, 'controls': [
            {'id': 'answer', 'tag': 'TEXTAREA', 'type': 'textarea', 'role': None, 'label': 'Why this role?*', 'value': 'Previously reviewed answer'},
            {'id': 'choice', 'tag': 'INPUT', 'type': 'text', 'role': 'combobox', 'label': 'Reviewed choice*', 'selected': ['No']},
        ]}
        return snapshot, self.page.locator('form')

    def test_recovery_replays_exact_answers_without_approving_or_submitting(self):
        snapshot, form = self.recovery_fixture()
        with patch.object(apply, 'STOP', threading.Event()):
            result = restore_snapshot(self.page, form, snapshot, apply, approved=True)
        self.assertEqual(result['not_restored'], [])
        self.assertFalse(result['continued'])
        self.assertFalse(result['submitted'])
        self.assertEqual(form.locator('textarea').input_value(), 'Previously reviewed answer')
        self.assertEqual(form.locator('.select__single-value').inner_text(), 'No')
        self.assertEqual(form.locator('#choice').input_value(), '')
        self.assertIsNone(self.page.evaluate('window.sent'))

    def test_recovery_requires_consent_and_exact_application_identity(self):
        snapshot, form = self.recovery_fixture()
        with self.assertRaisesRegex(ValueError, 'Explicit permission'):
            restore_snapshot(self.page, form, snapshot, apply)
        snapshot['url'] = snapshot['url'].replace('/123', '/456')
        with self.assertRaisesRegex(ValueError, 'different application'):
            restore_snapshot(self.page, form, snapshot, apply, approved=True)
        self.assertEqual(form.locator('textarea').input_value(), '')

    def test_recovery_removes_only_extra_default_chips_from_a_saved_choice(self):
        snapshot, form = self.recovery_fixture()
        form.locator('.select__value-container').evaluate('''root=>{
            for(const name of ['Default answer','No']){
                const chip=document.createElement('div');chip.className='select__multi-value';
                const label=document.createElement('div');label.className='select__multi-value__label';label.textContent=name;
                const remove=document.createElement('div');remove.className='select__multi-value__remove';
                remove.setAttribute('role','button');remove.textContent='×';remove.onclick=()=>chip.remove();
                chip.append(label,remove);root.append(chip);
            }
        }''')
        with patch.object(apply, 'STOP', threading.Event()):
            result = restore_snapshot(self.page, form, snapshot, apply, approved=True)
        self.assertEqual(result['not_restored'], [])
        self.assertEqual(form.locator('.select__multi-value__label').all_inner_texts(), ['No'])
        self.assertIsNone(self.page.evaluate('window.sent'))

    def test_recovery_refuses_changed_questions_before_overwriting_any_answers(self):
        snapshot, form = self.recovery_fixture()
        snapshot['controls'][1]['label'] = 'Different question'
        with self.assertRaisesRegex(ValueError, 'question changed'):
            restore_snapshot(self.page, form, snapshot, apply, approved=True)
        self.assertEqual(form.locator('textarea').input_value(), '')

    def test_recovery_restores_checkbox_choices_but_does_not_fill_composite_fieldset(self):
        url = 'https://job-boards.greenhouse.io/example/jobs/123'
        html = '<form>' + self.checkbox_group(selected=('Indeed',)) + '</form>'
        self.context.route('**/*', lambda route: route.fulfill(content_type='text/html', body=html))
        self.page.goto(url)
        controls = [{'tag': 'FIELDSET', 'id': 'sources', 'type': 'fieldset', 'role': None, 'label': ''}]
        controls += [{'tag': 'INPUT', 'id': f'source-{i}', 'type': 'checkbox', 'role': None, 'name': 'source[]',
                      'label': label, 'checked': i == 0} for i, label in enumerate(['LinkedIn', 'Glassdoor', 'Indeed'])]
        form = self.page.locator('form')
        with patch.object(apply, 'STOP', threading.Event()):
            result = restore_snapshot(self.page, form, {'url': url, 'controls': controls}, apply, approved=True)
        self.assertEqual(result['not_restored'], [])
        self.assertEqual(form.locator('input').evaluate_all('es=>es.map(e=>e.checked)'), [True, False, False])
        self.assertEqual(apply.native_form_issues(form), [])

    def lever_recovery_fixture(self):
        url = 'https://jobs.lever.co/example/86869664-1c38-4f5b-9a2a-555cdb565713/apply'
        html = '''<form><label>Full name<input name="name" required></label>
            <li class="application-question"><div class="application-label">Work authorization?</div>
            <label><input type="radio" name="cards[one][field0]" value="Yes" checked>Yes</label>
            <label><input type="radio" name="cards[one][field0]" value="No">No</label></li>
            <label>Voluntary survey<select name="eeo[answer]"><option value="">Select ...</option><option value="Decline">Decline</option></select></label>
            <input type="hidden" name="h-captcha-response" value="new-live-token">
            <button type="submit" onclick="window.sent=true">Submit</button></form>'''
        self.context.route('**/*', lambda route: route.fulfill(content_type='text/html', body=html))
        self.page.goto(url)
        snapshot = {'url': url, 'controls': [
            {'id': '', 'name': 'name', 'tag': 'INPUT', 'type': 'text', 'role': None, 'label': 'Full name', 'value': 'Reviewed Person'},
            {'id': '', 'name': 'cards[one][field0]', 'tag': 'INPUT', 'type': 'radio', 'role': None, 'label': 'Yes', 'question': 'Work authorization?', 'value': 'Yes', 'checked': False},
            {'id': '', 'name': 'cards[one][field0]', 'tag': 'INPUT', 'type': 'radio', 'role': None, 'label': 'No', 'question': 'Work authorization?', 'value': 'No', 'checked': True},
            {'id': '', 'name': 'eeo[answer]', 'tag': 'SELECT', 'type': 'select-one', 'role': None, 'label': 'Voluntary surveySelect ...Decline', 'value': 'Decline'},
            {'id': '', 'name': 'h-captcha-response', 'tag': 'INPUT', 'type': 'hidden', 'value': 'old-token'},
        ]}
        return snapshot, self.page.locator('form')

    def test_lever_restore_handles_named_fields_native_selects_and_radio_groups(self):
        snapshot, form = self.lever_recovery_fixture()
        with patch.object(apply, 'STOP', threading.Event()):
            result = restore_snapshot(self.page, form, snapshot, apply, approved=True)
        self.assertEqual(result['not_restored'], [])
        self.assertEqual(form.locator('[name="name"]').input_value(), 'Reviewed Person')
        self.assertFalse(form.locator('input[value="Yes"]').is_checked())
        self.assertTrue(form.locator('input[value="No"]').is_checked())
        self.assertEqual(form.locator('select').input_value(), 'Decline')
        self.assertEqual(form.locator('[name="h-captcha-response"]').input_value(), 'new-live-token')
        self.assertIsNone(self.page.evaluate('window.sent'))

    def test_lever_restore_does_not_reuse_yes_no_for_a_changed_question(self):
        snapshot, form = self.lever_recovery_fixture()
        form.locator('.application-label').evaluate("e=>e.textContent='Different question?'")
        with self.assertRaisesRegex(ValueError, 'question changed'):
            restore_snapshot(self.page, form, snapshot, apply, approved=True)
        self.assertEqual(form.locator('[name="name"]').input_value(), '')

    def test_lever_location_restoration_keeps_new_site_id_for_same_canonical_city(self):
        import json
        url = 'https://jobs.lever.co/example/86869664-1c38-4f5b-9a2a-555cdb565713/apply'
        city = 'Buenos Aires, Ciudad Autónoma de Buenos Aires, ARG'
        html = '''<form><label>Current location<input id="location-input" type="text" name="location"></label>
            <input type="hidden" id="selected-location" name="selectedLocation"></form>'''
        self.context.route('**/*', lambda route: route.fulfill(content_type='text/html', body=html))
        self.page.goto(url)
        form = self.page.locator('form')
        form.locator('#location-input').fill(city)
        form.locator('#selected-location').evaluate('(e,value)=>e.value=value', json.dumps({'name': city, 'id': 'new-site-issued-id'}))
        snapshot = {'url': url, 'leverLocation': {'name': city, 'id': 'old-site-issued-id'}, 'controls': [
            {'id': 'location-input', 'name': 'location', 'tag': 'INPUT', 'type': 'text', 'role': None, 'label': 'Current location', 'value': city}]}
        with patch.object(apply, 'STOP', threading.Event()):
            result = restore_snapshot(self.page, form, snapshot, apply, approved=True)
        self.assertEqual(result['not_restored'], [])
        self.assertEqual(json.loads(form.locator('#selected-location').input_value())['id'], 'new-site-issued-id')
