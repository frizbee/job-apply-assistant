"""Confirm the exact native/custom radio choice, never just any checked option."""
import os
import unittest
from unittest.mock import patch

from test_answer_safety import SafetyTest
import apply


class RadioStateTests(unittest.TestCase):
    def test_only_exactly_one_confirmed_boolean_choice_is_accepted(self):
        self.assertTrue(apply.radio_choice_confirmed([False, True], 1))
        self.assertTrue(apply.radio_choice_confirmed([True, False], 0))
        for states in [[True, False], [False, False], [True, True], [None, True], [False, None], [0, 1], []]:
            self.assertFalse(apply.radio_choice_confirmed(states, 1), states)

    def test_missing_or_invalid_state_never_counts_as_confirmation(self):
        for states in [None, True, "false,true", {"selected": 1}]:
            self.assertFalse(apply.radio_choice_confirmed(states, 1))
        for index in [-1, 2, None, True]:
            self.assertFalse(apply.radio_choice_confirmed([False, True], index))


@unittest.skipUnless(os.getenv("AUTOAPPLY_BROWSER_TESTS") == "1", "optional local Chrome radio-state tests")
class BrowserRadioSelectionTests(SafetyTest):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.pw = sync_playwright().start()
        cls.addClassCleanup(cls.pw.stop)
        cls.browser = cls.pw.chromium.launch(channel="chrome", headless=True)
        cls.addClassCleanup(cls.browser.close)

    def setUp(self):
        super().setUp()
        self.context = self.browser.new_context()
        self.addCleanup(self.context.close)
        self.context.route("**/*", lambda route: route.abort())
        self.page = self.context.new_page()
        self.page.set_default_timeout(1000)
        self.manual = self.enterContext(patch.object(apply, "record_manual"))
        self.record = self.enterContext(patch.object(apply, "record"))
        self.question = "Do you need visa sponsorship?"
        apply.CFG["confirmed_answers"] = {self.question: "No"}

    def form(self, content, modal=False):
        html = '<script>window.sent=0; window.clicks=0</script>'
        if modal:
            html += '<button type="button">Easy Apply</button><div role="dialog">'
        html += '<form onsubmit="event.preventDefault();window.sent++">' + content
        if modal:
            html += '<footer><button type="submit">Submit application</button></footer>'
        html += '</form>' + ('</div>' if modal else '')
        self.page.set_content(html)
        return self.page.locator('form')

    def native(self, no_attributes="", no_row_attributes="", yes_checked=True):
        return (f'<fieldset><legend>{self.question}</legend>'
                f'<div><input id="yes" type="radio" name="visa" value="yes" {"checked" if yes_checked else ""}>Yes</div>'
                f'<div {no_row_attributes}><input id="no" type="radio" name="visa" value="no" {no_attributes}>No</div></fieldset>')

    def custom(self, no_attributes="", yes_attributes="", group_attributes=""):
        return (f'<p>{self.question}</p><div role="radiogroup" {group_attributes}>'
                f'<div id="yes" role="radio" aria-checked="true" {yes_attributes}>Yes</div>'
                f'<div id="no" role="radio" aria-checked="false" {no_attributes}>No</div></div>')

    def test_preselected_opposite_native_radio_with_plain_div_is_corrected(self):
        form = self.form(self.native())
        self.assertEqual(apply.fill_radios(form), [])
        self.assertTrue(self.page.locator('#no').is_checked())
        self.assertFalse(self.page.locator('#yes').is_checked())

    def test_broken_custom_radio_click_is_unanswered_not_success(self):
        form = self.form(self.custom())
        self.assertEqual(apply.fill_radios(form), [self.question])
        self.assertEqual(self.page.locator('#yes').get_attribute('aria-checked'), 'true')
        self.assertEqual(self.page.locator('#no').get_attribute('aria-checked'), 'false')

    def test_custom_radio_with_real_aria_update_is_confirmed(self):
        form = self.form(self.custom(no_attributes='''onclick="document.getElementById('yes').setAttribute('aria-checked','false');this.setAttribute('aria-checked','true')"'''))
        self.assertEqual(apply.fill_radios(form), [])
        self.assertEqual(self.page.locator('#no').get_attribute('aria-checked'), 'true')

    def test_already_correct_native_or_custom_choice_is_not_clicked_again(self):
        form = self.form(self.native(no_attributes='checked onclick="window.clicks++"', yes_checked=False))
        self.assertEqual(apply.fill_radios(form), [])
        self.assertEqual(self.page.evaluate('window.clicks'), 0)
        form = self.form(self.custom(no_attributes='onclick="window.clicks++"'))
        self.page.locator('#yes').evaluate("e => e.setAttribute('aria-checked','false')")
        self.page.locator('#no').evaluate("e => e.setAttribute('aria-checked','true')")
        self.assertEqual(apply.fill_radios(form), [])
        self.assertEqual(self.page.evaluate('window.clicks'), 0)

    def test_target_must_be_exclusive_not_two_custom_radios_true(self):
        form = self.form(self.custom(no_attributes='''onclick="this.setAttribute('aria-checked','true')"'''))
        self.assertEqual(apply.fill_radios(form), [self.question])

    def test_missing_or_mixed_aria_state_is_not_confirmed(self):
        for state in [None, 'mixed']:
            form = self.form(self.custom())
            self.page.locator('#no').evaluate("(e, value) => value === null ? e.removeAttribute('aria-checked') : e.setAttribute('aria-checked',value)", state)
            self.assertEqual(apply.fill_radios(form), [self.question])

    def test_cancelled_native_click_cannot_keep_opposite_answer_silently(self):
        form = self.form(self.native(no_attributes='onclick="event.preventDefault()"'))
        self.assertEqual(apply.fill_radios(form), [self.question])
        self.assertFalse(self.page.locator('#no').is_checked())

    def test_disabled_opposite_radio_is_unanswered(self):
        form = self.form(self.native(no_attributes='disabled'))
        self.assertEqual(apply.fill_radios(form), [self.question])
        self.assertTrue(self.page.locator('#yes').is_checked())

    def test_hidden_native_radio_can_use_safe_working_row(self):
        form = self.form(self.native(no_attributes='style="display:none"',
            no_row_attributes='''onclick="document.getElementById('no').checked=true"'''))
        self.assertEqual(apply.fill_radios(form), [])
        self.assertTrue(self.page.locator('#no').is_checked())
        self.assertFalse(self.page.locator('#yes').is_checked())

    def test_timeout_after_successful_native_check_does_not_click_twice(self):
        from playwright.sync_api import Locator
        form = self.form(self.native(no_row_attributes='onclick="window.clicks++"'))
        original = Locator.check
        def check_then_timeout(locator, *args, **kwargs):
            original(locator, *args, **kwargs)
            raise apply.PWTimeout('response arrived after selection')
        with patch.object(Locator, 'check', new=check_then_timeout):
            self.assertEqual(apply.fill_radios(form), [])
        self.assertTrue(self.page.locator('#no').is_checked())
        self.assertEqual(self.page.evaluate('window.clicks'), 1)

    def test_custom_aria_and_backing_native_value_must_agree(self):
        form = self.form(f'''<fieldset><legend>{self.question}</legend>
            <div id="yes" role="radio" aria-checked="true"><input type="radio" name="visa" value="yes" checked>Yes</div>
            <div id="no" role="radio" aria-checked="false" onclick="document.getElementById('yes').setAttribute('aria-checked','false');this.setAttribute('aria-checked','true')"><input type="radio" name="visa" value="no">No</div>
            </fieldset>''')
        self.assertEqual(apply.fill_radios(form), [self.question])
        self.assertEqual(form.locator('input:checked').input_value(), 'yes')

    def test_custom_radio_with_consistent_backing_native_is_confirmed(self):
        form = self.form(f'''<fieldset><legend>{self.question}</legend>
            <div id="yes" role="radio" aria-checked="true"><input type="radio" name="visa" value="yes" checked>Yes</div>
            <div id="no" role="radio" aria-checked="false" onclick="document.getElementById('yes').setAttribute('aria-checked','false');this.setAttribute('aria-checked','true');this.querySelector('input').checked=true"><input type="radio" name="visa" value="no">No</div>
            </fieldset>''')
        self.assertEqual(apply.fill_radios(form), [])
        self.assertEqual(form.locator('input:checked').input_value(), 'no')

    def test_unmapped_native_radio_cannot_be_ignored_by_custom_options(self):
        content = self.custom(no_attributes='onclick="window.clicks++"')
        content = content.replace('</div></div>', '</div><input type="radio" name="visa" value="unmapped" checked></div>')
        form = self.form(content)
        self.assertEqual(apply.fill_radios(form), [self.question])
        self.assertEqual(self.page.evaluate('window.clicks'), 0)

    def test_changed_option_labels_require_manual_review(self):
        form = self.form(self.custom(no_attributes='''onclick="document.getElementById('yes').setAttribute('aria-checked','false');this.setAttribute('aria-checked','true');this.textContent='Another answer'"'''))
        self.assertEqual(apply.fill_radios(form), [self.question])

    def test_unverified_radio_blocks_live_and_test_application(self):
        for dry_run in [True, False]:
            with self.subTest(dry_run=dry_run), patch.object(apply, 'DRY_RUN', dry_run):
                self.form(self.custom(group_attributes='aria-required="true"'), modal=True)
                self.assertEqual(apply.apply_to_job(self.page, 'fixture', 'Engineer', 'Example'), 'manual_review')
                self.assertEqual(self.page.evaluate('window.sent'), 0)
        self.record.assert_not_called()
        self.assertEqual(self.manual.call_count, 2)

    def test_choice_reset_after_filling_is_caught_before_submit(self):
        form = self.form(self.native())
        self.assertEqual(apply.fill_radios(form), [])
        self.page.locator('#yes').evaluate('e => e.checked = true')
        self.assertTrue(apply.require_manual_review(self.page, form, [], 'fixture', 'Engineer', 'Example'))
        self.assertEqual(self.page.evaluate('window.sent'), 0)
        self.manual.assert_called_once()

    def test_verified_choice_passes_final_recheck_without_extra_clicks(self):
        form = self.form(self.custom(no_attributes='''onclick="window.clicks++;document.getElementById('yes').setAttribute('aria-checked','false');this.setAttribute('aria-checked','true')"'''))
        self.assertEqual(apply.fill_radios(form), [])
        self.assertFalse(apply.require_manual_review(self.page, form, [], 'fixture', 'Engineer', 'Example'))
        self.assertEqual(self.page.evaluate('window.clicks'), 1)
        self.manual.assert_not_called()

    def test_preselected_radio_outside_known_group_is_not_silently_accepted(self):
        form = self.form('<input type="radio" name="unknown" value="yes" checked>')
        self.assertTrue(apply.require_manual_review(self.page, form, [], 'fixture', 'Engineer', 'Example'))
        self.manual.assert_called_once()

    def yesno(self, no_action=''):
        return (f'<p>{self.question}</p><div><button id="yes" type="button" aria-pressed="true">Yes</button>'
                f'<button id="no" type="button" aria-pressed="false" {no_action}>No</button></div>')

    def test_plain_yesno_broken_click_is_also_unanswered(self):
        form = self.form(self.yesno())
        self.assertEqual(apply.fill_yesno(form), [self.question])
        self.assertEqual(self.page.locator('#yes').get_attribute('aria-pressed'), 'true')

    def test_plain_yesno_verified_choice_is_not_clicked_twice(self):
        form = self.form(self.yesno('''onclick="window.clicks++;document.getElementById('yes').setAttribute('aria-pressed','false');this.setAttribute('aria-pressed','true')"'''))
        self.assertEqual(apply.fill_yesno(form), [])
        self.assertEqual(apply.fill_yesno(form), [])
        self.assertEqual(self.page.evaluate('window.clicks'), 1)

    def test_css_selected_class_without_semantic_state_is_not_confirmation(self):
        form = self.form(f'<p>{self.question}</p><div><button type="button">Yes</button>'
            '<button type="button" onclick="this.className=\'selected\'">No</button></div>')
        self.assertEqual(apply.fill_yesno(form), [self.question])
        self.assertEqual(self.page.evaluate('window.sent'), 0)

    def test_plain_yesno_aria_cannot_disagree_with_nested_native_radio(self):
        form = self.form(f'''<p>{self.question}</p><div>
            <button id="yes" type="button" aria-pressed="true"><input type="radio" name="visa" value="yes" checked style="pointer-events:none">Yes</button>
            <button id="no" type="button" aria-pressed="false" onclick="document.getElementById('yes').setAttribute('aria-pressed','false');this.setAttribute('aria-pressed','true')"><input type="radio" name="visa" value="no" style="pointer-events:none">No</button>
            </div>''')
        self.assertEqual(apply.fill_yesno(form), [self.question])
        self.assertEqual(form.locator('input:checked').input_value(), 'yes')

    def test_unverified_yesno_blocks_external_submit_in_both_modes(self):
        self.form(self.yesno(), modal=True)
        html = self.page.content()
        self.context.route('https://jobs.lever.co/**', lambda route: route.fulfill(content_type='text/html', body=html))
        with patch.object(apply, 'EXTERNAL', True), patch.object(apply, 'EXT_STATE', {'count': 0, 'last': 0.0}):
            for dry_run in [True, False]:
                with self.subTest(dry_run=dry_run), patch.object(apply, 'DRY_RUN', dry_run):
                    status = apply.apply_form(self.page, 'fixture', 'Engineer', 'Example', 'https://jobs.lever.co/example/job/apply')
                    self.assertEqual(status, 'manual_review')
                    self.assertEqual(self.page.evaluate('window.sent'), 0)
        self.record.assert_not_called()
