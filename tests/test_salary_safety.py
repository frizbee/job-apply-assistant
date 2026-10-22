"""Salary answers must have explicit units and a reliable conversion."""
import os
import json
import unittest
from unittest.mock import MagicMock, patch

from test_answer_safety import SafetyTest
import apply


class SalarySafetyTests(SafetyTest):
    def setUp(self):
        super().setUp()
        self.fx = self.enterContext(patch.object(apply, 'rates', return_value={}))

    def test_missing_fx_never_relabels_amount(self):
        self.assertEqual(apply.salary_answer('Annual salary in GBP'), '')
        self.assertEqual(apply.answer('Annual salary in GBP', numeric=True), [])

    def test_explicit_period_overrides_rate(self):
        for period, expected in [('Annual', '60000'), ('Monthly', '5000'), ('Hourly', '28.85'), ('Daily', '272.73')]:
            self.assertEqual(apply.salary_answer(f'{period} salary rate in USD'), expected)

    def test_missing_unknown_or_conflicting_units_require_review(self):
        for q in ['Salary', 'Annual salary', 'Salary rate in USD', 'Weekly salary in USD',
                  'Annual salary in AUD', 'Annual salary in XYZ', 'Annual salary in dollars',
                  'Annual salary in $', 'Annual salary in USD or EUR', 'Annual/monthly salary in USD',
                  'Annual salary in AUD ($)', 'Annual salary in USD / AUD', 'Aylık maaş',
                  'Annual salary in Australian dollars (USD)', 'Weekly or annual salary in USD',
                  'Semi-annual salary in USD', 'Semi-monthly salary in USD',
                  'Annual salary in USD or XYZ', 'Annual salary in USD / ¥']:
            with self.subTest(q=q):
                self.assertEqual(apply.salary_answer(q), '')

    def test_valid_fx_conversion(self):
        self.fx.return_value = {'USD': 1, 'GBP': .8}
        self.assertEqual(apply.salary_answer('Annual salary in GBP'), '48000')
        apply.SETTINGS.update(currency='GBP', salary='48000')
        self.assertEqual(apply.salary_answer('Monthly salary in USD'), '5000')

    def test_invalid_fx_values_are_rejected(self):
        for value in [0, -1, float('nan'), float('inf'), None, True, 'bad']:
            for key in ['USD', 'GBP']:
                self.fx.return_value = {'USD': 1, 'GBP': .8, key: value}
                self.assertEqual(apply.salary_answer('Annual salary in GBP'), '')

    def test_missing_invalid_or_zero_amount_never_becomes_positive(self):
        for value in ['', None, 0, '0', '-100', 'bad', 'nan', 'inf', True]:
            apply.SETTINGS['salary'] = value
            self.assertEqual(apply.salary_answer('Annual salary in USD'), '')

    def test_small_amount_is_not_rounded_up_to_invented_minimum(self):
        apply.SETTINGS['salary'] = '12'
        self.assertEqual(apply.salary_answer('Annual salary in USD'), '12')
        self.assertEqual(apply.salary_answer('Monthly salary in USD'), '1')

    def test_thousands_preserve_fraction(self):
        apply.SETTINGS['salary'] = '60500'
        self.assertEqual(apply.salary_answer('Annual salary in USD (in thousands)'), '60.5')

    def test_explicit_monthly_override_needs_no_fx(self):
        apply.SETTINGS['monthly_salary_by_currency'] = {'GBP': 2345.5}
        self.assertEqual(apply.salary_answer('Annual salary in GBP'), '28146')
        self.assertEqual(apply.salary_answer('Monthly salary in GBP'), '2345.5')
        self.fx.assert_not_called()

    def test_invalid_override_does_not_fall_back_to_another_expectation(self):
        for value in [None, 0, -10, 'nan', True]:
            apply.SETTINGS['monthly_salary_by_currency'] = {'USD': value}
            self.assertEqual(apply.salary_answer('Annual salary in USD'), '')

    def test_specific_currency_names_and_symbols(self):
        for q, currency in [('Annual salary in Canadian dollars ($)', 'CAD'),
                            ('Yıllık maaş (TL)', 'TRY'), ('Monthly salary in €', 'EUR'),
                            ('Annual salary in £', 'GBP')]:
            apply.SETTINGS.update(currency=currency, salary='60000')
            self.assertEqual(apply.salary_answer(q), '5000' if 'Monthly' in q else '60000')

    def test_explicit_per_and_slash_periods(self):
        for unit, expected in [('year', '60000'), ('month', '5000'), ('hour', '28.85'), ('day', '272.73')]:
            for prefix in ['per ', '/ ', '/']:
                self.assertEqual(apply.salary_answer(f'Salary in USD {prefix}{unit}'), expected)

    def test_exact_confirmed_answer_still_has_precedence(self):
        apply.CFG['confirmed_answers'] = {'Salary rate?': '123'}
        self.assertEqual(apply.answer('Salary rate?', numeric=True), ['123'])


class ExchangeRateTests(SafetyTest):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.object(apply, 'RATES', {}))
        self.request = self.enterContext(patch('urllib.request.urlopen'))

    def response(self, payload):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(payload).encode()
        return response

    def test_unavailable_providers_leave_rates_empty(self):
        self.request.side_effect = OSError('offline')
        self.assertEqual(apply.rates(), {})
        self.assertEqual(apply.salary_answer('Annual salary in GBP'), '')

    def test_wrong_base_or_malformed_rates_do_not_enter_cache(self):
        for payload in [{'base': 'EUR', 'rates': {'GBP': .8}}, {'rates': {'GBP': .8}},
                        {'base': 'USD', 'rates': []}, {'base': 'USD', 'rates': {'USD': 2, 'GBP': .8}}]:
            self.request.return_value = self.response(payload)
            self.assertEqual(apply.rates(), {})

    def test_only_positive_finite_rates_are_cached(self):
        self.request.return_value = self.response({'base_code': 'USD', 'rates': {
            'USD': 1, 'GBP': .8, 'EUR': 0, 'CHF': -1, 'TRY': float('nan'), 'CAD': True}})
        self.assertEqual(apply.rates(), {'USD': 1, 'GBP': .8})
        self.assertEqual(apply.salary_answer('Annual salary in GBP'), '48000')


@unittest.skipUnless(os.getenv('AUTOAPPLY_BROWSER_TESTS') == '1', 'optional local Chrome salary tests')
class BrowserSalaryTests(SafetyTest):
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
        self.enterContext(patch.object(apply, 'rates', return_value={}))
        self.manual = self.enterContext(patch.object(apply, 'record_manual'))
        self.enterContext(patch.object(apply, 'record'))

    def form(self, question):
        self.page.set_content(f'''<script>window.sent=0</script><button type="button">Easy Apply</button>
            <div role="dialog"><form onsubmit="event.preventDefault();window.sent++">
            <label for="salary">{question}</label><input id="salary" type="number" required step="any">
            <footer><button type="submit">Submit application</button></footer></form></div>''')

    def test_unavailable_salary_blocks_submission_in_both_modes(self):
        for q in ['Annual salary in GBP', 'Salary rate in USD', 'Annual salary in AUD']:
            for dry_run in [True, False]:
                with self.subTest(q=q, dry_run=dry_run), patch.object(apply, 'DRY_RUN', dry_run):
                    self.form(q)
                    self.assertEqual(apply.apply_to_job(self.page, 'fixture', 'Engineer', 'Example'), 'manual_review')
                    self.assertEqual(self.page.locator('#salary').input_value(), '')
                    self.assertEqual(self.page.evaluate('window.sent'), 0)

    def test_explicit_annual_rate_fills_annual_amount_in_test_mode(self):
        self.form('Annual salary rate in USD')
        with patch.object(apply, 'DRY_RUN', True):
            apply.apply_to_job(self.page, 'fixture', 'Engineer', 'Example')
        self.assertEqual(self.page.locator('#salary').input_value(), '60000')
        self.assertEqual(self.page.evaluate('window.sent'), 0)

    def test_unavailable_fx_blocks_external_ats_submission(self):
        self.form('Annual salary in GBP')
        html = self.page.content()
        self.context.route('https://jobs.lever.co/**', lambda route: route.fulfill(content_type='text/html', body=html))
        with patch.object(apply, 'EXTERNAL', True), patch.object(apply, 'EXT_STATE', {'count': 0, 'last': 0.0}):
            for mode in [True, False]:
                with patch.object(apply, 'DRY_RUN', mode):
                    status = apply.apply_form(self.page, 'fixture', 'Engineer', 'Example', 'https://jobs.lever.co/example/job/apply')
                    self.assertEqual(status, 'manual_review')
                    self.assertEqual(self.page.evaluate('window.sent'), 0)
