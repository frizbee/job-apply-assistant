"""Phone boundaries are reviewed input, not inferred country preferences."""
import json
import os
import unittest

from phone_contact import join_phone, split_phone


class PhoneContactTests(unittest.TestCase):
    def test_roundtrip_preserves_local_leading_zero(self):
        self.assertEqual(join_phone('+44', '020 1234 5678'), '+44 02012345678')
        self.assertEqual(split_phone('+44 02012345678'), ('+44', '02012345678'))

    def test_explicit_legacy_boundary_and_formatting_are_supported(self):
        self.assertEqual(split_phone('+1 (555) 123-4567'), ('+1', '5551234567'))
        self.assertEqual(split_phone('+90-555 123 4567'), ('+90', '5551234567'))

    def test_unsplit_number_does_not_guess_calling_code(self):
        for value in ['+905551234567', '+15551234567', '5551234567', '', '+0 123', '+1234 567']:
            self.assertEqual(split_phone(value), ('', ''))

    def test_non_text_and_invalid_values_rejected_without_echoing_personal_values(self):
        for code, number in [('+1', None), (1, '123'), ('+1', 'PRIVATE'), ('+1', '()')]:
            with self.assertRaises(ValueError) as error:
                join_phone(code, number)
            self.assertNotIn('PRIVATE', str(error.exception))


@unittest.skipUnless(os.getenv('AUTOAPPLY_BROWSER_TESTS') == '1', 'local GUI fixture')
class PhoneGuiTests(unittest.TestCase):
    def test_gui_displays_explicit_parts_clears_stale_parts_and_sends_both(self):
        import gui
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel='chrome', headless=True)
            page = browser.new_page()
            sent = []
            def route(request):
                if request.request.url.endswith('/api/state'):
                    request.fulfill(content_type='application/json', body=json.dumps({
                        'settings': {}, 'profile': None, 'default_locations': ['User region']}))
                elif request.request.url.endswith('/api/start'):
                    sent.append(request.request.post_data_json)
                    request.fulfill(content_type='application/json', body='{"error":"invalid_profile"}')
                else:
                    request.fulfill(content_type='text/html', body=gui.PAGE.replace('__TOKEN__', 'fixture'))
            page.route('**/*', route)
            page.goto('http://gui.test/')
            page.wait_for_function('typeof S !== "undefined" && S.settings')
            page.evaluate('fill({phone_intl:"+1 555 123 4567"})')
            self.assertEqual(page.locator('#phone_country_code').input_value(), '+1')
            self.assertEqual(page.locator('#phone_national').input_value(), '5551234567')
            page.evaluate('fill({phone_intl:"+905551234567"})')
            self.assertEqual(page.locator('#phone_country_code').input_value(), '')
            self.assertEqual(page.locator('#phone_national').input_value(), '')
            self.assertEqual(page.locator('#phone_intl').input_value(), '+905551234567')
            page.locator('#phone_country_code').fill('+90')
            page.locator('#phone_national').fill('5551234567')
            page.locator('#start').click()
            page.wait_for_function('document.getElementById("m3").textContent.length > 0')
            self.assertEqual(sent[0]['phone_country_code'], '+90')
            self.assertEqual(sent[0]['phone_national'], '5551234567')
            browser.close()
