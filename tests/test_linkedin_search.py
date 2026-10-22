"""Scoped SDUI cards, exact user geography and non-repeating pagination."""
import copy
import html
import json
import os
import threading
import unittest
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

import apply
import linkedin_search as search
from test_answer_safety import SafetyTest


class SearchFlowTests(SafetyTest):
    def test_native_url_preserves_user_filters_and_requires_observed_numeric_geo(self):
        original = apply.search_url('C++ & Ruby', 'User region')
        url = search.native_url(original, '123')
        params = parse_qs(urlparse(url).query)
        self.assertEqual(params['keywords'], ['C++ & Ruby'])
        self.assertEqual(params['geoId'], ['123'])
        self.assertNotIn('location', params)
        self.assertEqual(params['f_TPR'], parse_qs(urlparse(original).query)['f_TPR'])
        with self.assertRaises(ValueError):
            search.native_url(original, '123&geoId=evil')

    def test_pagination_uses_next_flag_not_assumption_about_number_of_cards(self):
        apply.CFG['search'].update(keywords=['User role'], pages_per_search=2)
        apply.SETTINGS['locations'] = ['User region']
        with patch.object(search, 'LinkedInSearch') as factory:
            factory.return_value.search_page.side_effect = [
                {'ids': ['1'], 'has_next': True}, {'ids': ['2'], 'has_next': False}]
            self.assertEqual(list(apply.job_lists(object())), [['1'], ['2']])
        self.assertEqual([c.args[2] for c in factory.return_value.search_page.call_args_list], [0, 1])
        factory.return_value.close.assert_called_once()

    def test_unsupported_regions_are_not_replaced_with_cv_country_or_other_options(self):
        apply.CFG['search'].update(keywords=['First', 'Second'], pages_per_search=1)
        apply.SETTINGS['locations'] = ['Unsupported', 'Supported']
        before = copy.deepcopy((apply.CFG, apply.SETTINGS, apply.PROFILE))
        def open_page(keyword, region, index):
            if region == 'Unsupported':
                raise search.UnsupportedLocation('No exact choice')
            return {'ids': ['123'], 'has_next': False}
        with patch.object(search, 'LinkedInSearch') as factory:
            factory.return_value.search_page.side_effect = open_page
            self.assertEqual(list(apply.job_lists(object())), [['123'], ['123']])
        self.assertEqual(len(factory.return_value.search_page.call_args_list), 3)
        self.assertEqual((apply.CFG, apply.SETTINGS, apply.PROFILE), before)
        self.assertEqual(apply.METRICS.snapshot()['searches_failed'], 2)

    def test_search_tab_is_closed_if_worker_stops_consuming(self):
        apply.CFG['search'].update(keywords=['User role'], pages_per_search=1)
        apply.SETTINGS['locations'] = ['User region']
        with patch.object(search, 'LinkedInSearch') as factory:
            factory.return_value.search_page.return_value = {'ids': ['1'], 'has_next': False}
            generator = apply.job_lists(object())
            next(generator)
            generator.close()
        factory.return_value.close.assert_called_once()


@unittest.skipUnless(os.getenv('AUTOAPPLY_BROWSER_TESTS') == '1', 'offline LinkedIn DOM fixtures')
class SearchBrowserTests(SafetyTest):
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
        self.worker = self.ctx.new_page()
        self.enterContext(patch.object(apply, 'STOP', threading.Event()))
        self.enterContext(patch.object(apply, 'DEADLINE', float('inf')))
        self.repeated = False
        self.raw_html = None
        self.requests = []
        self.ctx.route('**/*', self.route)
        self.engine = search.LinkedInSearch(self.worker, apply)
        self.addCleanup(self.engine.close)
        self.engine.wait = lambda ms: self.engine.page.wait_for_timeout(min(ms, 10))

    def route(self, route):
        self.requests.append(route.request.url)
        if self.raw_html is not None:
            route.fulfill(content_type='text/html', body=self.raw_html)
            return
        params = parse_qs(urlparse(route.request.url).query)
        keyword = params.get('keywords', [''])[0]
        applied = params.get('geoId') == ['111']
        region = 'Latin America' if applied else 'Greater Buenos Aires'
        body = f'''<meta charset="utf-8"><input componentkey="semanticSearchBox" value="{html.escape(keyword, quote=True)}">
        <div componentkey="searchResultsHeaderComponent"><p>2 results</p><div role="button" tabindex="0" onclick="document.getElementById('popup').style.display='block'">
        <span aria-label="Location {region}">{region}</span></div></div>
        <div id="popup" style="display:none"><div role="textbox" contenteditable="true" aria-label="Location" oninput="suggest(this.innerText)"></div><div id="semanticSearchBoxLocationList"></div></div>
        <div componentkey="JobsSearchFilters"><div><input type="checkbox" id="remote" aria-label="Filter by Remote" checked><label for="remote">Remote</label></div>
        <div><input type="checkbox" id="easy" aria-label="Filter by Easy Apply"><label for="easy">Easy Apply</label></div></div>
        <aside><a href="/jobs/view/999/">Selected detail / recommendation</a><div componentkey="job-card-component-ref-999" role="button">Not a result</div></aside>
        <div componentkey="SearchResultsMainContent"><section id="cards"><div componentkey="job-card-component-ref-1" role="button"><div componentkey="job-card-component-ref-1">First</div></div><div componentkey="job-card-component-ref-2">Second</div></section>
        <button type="button" data-testid="pagination-controls-next-button-visible" onclick="nextPage()">Next</button>
        <button type="button" aria-label="Page 1" aria-current="true">1</button><button type="button" aria-label="Page 2" aria-current="false">2</button></div>
        <script>
        function suggest(value){{let out=document.getElementById('semanticSearchBoxLocationList');out.innerHTML='';
            const label=value==='Latin America'?'Latin America':value==='Europe'?'European Union':null;
            if(label){{let a=document.createElement('a');a.textContent=label;a.href='https://www.linkedin.com/jobs/search-results/?geoId=111';out.appendChild(a)}}}}
        function nextPage(){{
            if(!{str(self.repeated).lower()})document.getElementById('cards').innerHTML='<div componentkey="job-card-component-ref-3">Third</div><div componentkey="job-card-component-ref-4">Fourth</div>';
            document.querySelector('[aria-label="Page 1"]').setAttribute('aria-current','false');document.querySelector('[aria-label="Page 2"]').setAttribute('aria-current','true');}}
        </script>'''
        route.fulfill(content_type='text/html', body=body)

    def test_sdui_cards_deduplicate_and_exclude_detail_and_recommendations(self):
        result = self.engine.search_page('Senior Ruby Developer', 'Latin America')
        self.assertEqual(result['ids'], ['1', '2'])
        self.assertTrue(result['has_next'])
        self.assertEqual(self.engine.location_label(), 'Latin America')
        self.assertEqual(self.engine.page.url.split('geoId=')[1].split('&')[0], '111')

    def test_unsupported_europe_is_not_silently_replaced_with_eu(self):
        with self.assertRaises(search.UnsupportedLocation):
            self.engine.search_page('Senior Ruby Developer', 'Europe')
        self.assertFalse(any('geoId=111' in u for u in self.requests))
        self.assertEqual(self.engine.location_label(), 'Greater Buenos Aires')

    def test_next_page_is_collected_on_own_tab_even_after_worker_opens_a_job(self):
        first = self.engine.search_page('Senior Ruby Developer', 'Latin America')
        self.worker.goto('https://www.linkedin.com/jobs/view/987/')
        second = self.engine.search_page('Senior Ruby Developer', 'Latin America', 1)
        self.assertEqual(first['ids'], ['1', '2'])
        self.assertEqual(second['ids'], ['3', '4'])

    def test_repeated_pagination_is_stopped(self):
        self.repeated = True
        self.engine.search_page('Senior Ruby Developer', 'Latin America')
        with self.assertRaises(search.SearchUnavailable):
            self.engine.search_page('Senior Ruby Developer', 'Latin America', 1)

    def test_legacy_scoped_anchors_work_without_global_link_fallback(self):
        self.raw_html = '''<div class="jobs-search-results-list"><div data-job-id="123">Legacy</div><a href="/jobs/view/456/">Another</a></div>
            <aside><a href="/jobs/view/999/">Recommendation</a></aside>'''
        self.worker.goto('https://www.linkedin.com/jobs/search/')
        self.assertEqual(self.worker.evaluate(apply.SCRAPE_IDS_JS), ['123', '456'])

    def test_late_hydration_is_waited_for(self):
        self.raw_html = '''<div componentkey="SearchResultsMainContent" id="results"></div><script>
            setTimeout(()=>document.getElementById('results').innerHTML='<div componentkey="job-card-component-ref-123">Job</div>',300)</script>'''
        self.worker.goto('https://www.linkedin.com/jobs/search-results/')
        self.assertEqual(self.worker.evaluate(apply.SCRAPE_IDS_JS), ['123'])

    def test_hidden_cards_and_job_detail_without_search_root_are_not_results(self):
        self.raw_html = '<a href="/jobs/view/999/">Selected detail</a>'
        self.worker.goto('https://www.linkedin.com/jobs/search-results/')
        result = self.worker.evaluate(search.COLLECT_JS)
        self.assertEqual(result['ids'], [])
        self.assertEqual(result['root_count'], 0)
        self.raw_html = '<div componentkey="SearchResultsMainContent"><div componentkey="job-card-component-ref-1">Job</div><div componentkey="job-card-component-ref-2" style="display:none">Hidden</div></div>'
        self.worker.goto('https://www.linkedin.com/jobs/search-results/')
        self.assertEqual(self.worker.evaluate(apply.SCRAPE_IDS_JS), ['1'])

    def test_native_easy_apply_filter_is_set_without_touching_shared_settings(self):
        with patch.object(apply, 'EXTERNAL', False):
            before = copy.deepcopy((apply.CFG, apply.SETTINGS))
            self.engine.search_page('Senior Ruby Developer', 'Latin America')
        self.assertTrue(self.engine.page.locator('#easy').is_checked())
        self.assertEqual((apply.CFG, apply.SETTINGS), before)
