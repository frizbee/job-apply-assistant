"""Wellfound-only native search contract and isolation from other providers."""
import copy
import json
import os
import threading
import unittest
from unittest.mock import MagicMock, patch

import apply
import sources
import wellfound_search as native
from test_answer_safety import SafetyTest, node


def configuration(keyword='Senior Ruby on Rails Developer'):
    return {'page': 1, 'keywords': [keyword], 'remotePreference': 'REMOTE_ONLY',
            'equity': {'min': None, 'max': None}, 'salary': {'min': None, 'max': None}}


def payload(applied=False):
    listing = {'id': '123', 'slug': 'senior-ruby-developer', 'title': 'Senior Ruby Developer',
               'description': 'Build APIs with Ruby.', 'currentUserApplied': applied,
               'liveStartAt': 1792598400, 'remoteConfig': {'kind': 'REMOTE'}, 'remote': True,
               'acceptedRemoteLocationNames': ['Europe']}
    return {'data': {'talent': {'jobSearchResults': {'hasNextPage': False,
            'startups': {'edges': [{'node': {'name': 'Example', 'highlightedJobListings': [listing]}}]}}}}}


class NativeContractTests(unittest.TestCase):
    def test_full_gui_phrase_is_required_verbatim_not_inferred_taxonomy(self):
        config = configuration()
        self.assertTrue(native.verified_configuration(config, config['keywords'][0], 1, True))
        self.assertFalse(native.verified_configuration(config, 'Ruby on Rails Developer', 1, True))
        self.assertFalse(native.verified_configuration(config, config['keywords'][0], 2, True))

    def test_undisclosed_restrictions_are_rejected(self):
        keyword = configuration()['keywords'][0]
        for field, value in [('roleTagIds', ['old-role']), ('locationTagIds', ['old-city']),
                             ('jobTypes', ['full_time']), ('salary', {'min': 100}), ('skillTagIds', ['old-skill'])]:
            self.assertFalse(native.verified_configuration({**configuration(), field: value}, keyword, 1, True))
        config = {**configuration(), 'remoteCompanyLocationTagIds': ['observed-site-region']}
        self.assertFalse(native.verified_configuration(config, keyword, 1, True))
        self.assertTrue(native.verified_configuration(config, keyword, 1, True, ['observed-site-region']))

    def test_only_provider_result_listings_are_read(self):
        data = payload()
        data['recommendations'] = [{'id': 'wrong', 'title': 'Marketing Lead'}]
        jobs, more = native.result_jobs(data, 'Senior Ruby Developer', 1)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]['id'], 'wellfound:123-senior-ruby-developer')
        self.assertEqual(jobs[0]['company'], 'Example')
        self.assertEqual(jobs[0]['search_provenance']['keyword'], 'Senior Ruby Developer')
        self.assertFalse(more)

    def test_invalid_identity_errors_or_unknown_applied_state_fail_closed(self):
        for change in [{'id': '../evil'}, {'slug': 'https://evil.test'}, {'currentUserApplied': None}]:
            data = payload()
            data['data']['talent']['jobSearchResults']['startups']['edges'][0]['node']['highlightedJobListings'][0].update(change)
            with self.assertRaises(native.SearchUnavailable):
                native.result_jobs(data, 'Engineer', 1)
        with self.assertRaises(native.SearchUnavailable):
            native.result_jobs({'errors': [{'message': 'ignored provider text'}]}, 'Engineer', 1)
        with self.assertRaises(native.SearchUnavailable):
            native.result_jobs([], 'Engineer', 1)


class ProviderIsolationTests(SafetyTest):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.object(sources, 'core', apply))
        self.enterContext(patch.object(sources, 'LOGGED_IN', {'indeed': True, 'wellfound': False}))
        self.enterContext(patch.object(apply, 'DEADLINE', float('inf')))
        self.enterContext(patch.object(apply, 'STOP', threading.Event()))
        self.loader = self.enterContext(patch.object(apply, 'load_applied', return_value={}))
        self.search = MagicMock(company_region_label='User site preference')
        self.search.search.side_effect = lambda keyword: native.result_jobs(payload(), keyword, 1)
        self.enterContext(patch.object(native, 'NativeSearch', return_value=self.search))

    def test_native_keywords_never_modify_gui_or_shared_config(self):
        apply.CFG['search']['keywords'] = ['Senior Ruby on Rails Developer', 'Senior Backend Engineer']
        before = copy.deepcopy((apply.CFG, apply.SETTINGS, apply.PROFILE))
        other_sources = {name: fn for name, fn in sources.SOURCES.items() if name != 'wellfound'}
        jobs = list(sources.wellfound(node()))
        self.assertEqual(len(jobs), 1)  # Dedupe the same native ID across queries.
        self.assertEqual([call.args[0] for call in self.search.search.call_args_list], before[0]['search']['keywords'])
        self.assertEqual((apply.CFG, apply.SETTINGS, apply.PROFILE), before)
        self.assertEqual({name: fn for name, fn in sources.SOURCES.items() if name != 'wellfound'}, other_sources)
        self.search.close.assert_called_once()
        self.assertTrue(sources.LOGGED_IN['wellfound'])
        self.assertTrue(sources.LOGGED_IN['indeed'])

    def test_provider_applied_and_renamed_slug_uncertain_history_are_protected(self):
        for history, provider_applied in [({}, True),
                ({'wellfound:123-old-slug': {'status': 'submission_unconfirmed'}}, False),
                ({'wellfound:123-old-slug': {'status': 'applied'}}, False)]:
            self.loader.return_value = history
            self.search.search.side_effect = lambda keyword: native.result_jobs(payload(provider_applied), keyword, 1)
            self.assertEqual(list(sources.wellfound(node())), [])

    def test_unavailable_native_search_uses_only_existing_strict_public_fallback(self):
        self.search.start.side_effect = native.SearchUnavailable('login required')
        with patch.object(sources, '_wellfound_public', return_value=iter([])) as fallback:
            self.assertEqual(list(sources.wellfound(node())), [])
        fallback.assert_called_once()
        self.search.search.assert_not_called()

    def test_failed_native_query_never_dispatches_a_different_provider(self):
        self.search.search.side_effect = native.SearchUnavailable('query mismatch')
        with patch.object(sources, '_wellfound_public') as fallback, patch.object(sources, 'indeed') as other:
            self.assertEqual(list(sources.wellfound(node())), [])
        fallback.assert_not_called()
        other.assert_not_called()


@unittest.skipUnless(os.getenv('AUTOAPPLY_BROWSER_TESTS') == '1', 'offline native-search Chrome fixture')
class NativeBrowserTests(SafetyTest):
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
        self.enterContext(patch.object(apply, 'STOP', threading.Event()))
        self.addCleanup(self.ctx.close)
        self.page = self.ctx.new_page()
        self.page.set_default_timeout(2000)
        self.requests = []
        self.html = '''<h1 onclick="document.getElementById('menu').style.display='none'">Search for jobs</h1>
        <div role="combobox" aria-labelledby="RemotePrefereceSelectField-label"><button type="button" onclick="document.getElementById('menu').style.display='block'">Region</button></div>
        <div id="menu" role="listbox" aria-labelledby="RemotePrefereceSelectField-label" style="display:none">
        <input type="radio" id="Include remote jobs" name="remote" checked><label for="Include remote jobs" onclick="cfg.remotePreference='REMOTE_OPEN'">Include remote jobs</label>
        <input type="radio" id="Only remote jobs" name="remote"><label for="Only remote jobs" onclick="cfg.remotePreference='REMOTE_ONLY'">Only remote jobs</label><button type="button">User site region</button></div>
        <div data-test="RoleSelectWrapper"><button data-test="SearchBar-RoleSelect-FocusButton" type="button">Old role</button>
        <button type="button" class="select__multi-value__remove" onclick="delete cfg.roleTagIds;this.remove()">Remove role</button></div>
        <div data-test="SearchBar"><div class="locationWrapper"><button type="button" class="locationField">Old city</button>
        <button type="button" class="select__multi-value__remove" onclick="delete cfg.locationTagIds;this.remove()">Remove city</button></div></div>
        <button type="button" data-test="ActiveFilterSummary-FilterButton--jobTypes" onclick="delete cfg.jobTypes;this.remove()">Clear Full Time</button>
        <button type="button" onclick="document.getElementById('panel').style.display='block'">Filters</button>
        <div id="panel" role="dialog" aria-modal="true" style="display:none"><div data-test="KeywordsFilterField--keywords"><h5>Included keywords</h5><div id="tokens"></div><input data-test="KeywordsFilterField--keywords--input" onkeydown="if(event.key==='Enter'){cfg.keywords=[this.value];document.getElementById('tokens').textContent=this.value;run()}"></div>
        <button type="button" data-test="SearchBar-ViewResultsButton" onclick="document.getElementById('panel').style.display='none'">View results</button></div>
        <script>const cfg={page:1,roleTagIds:['old'],locationTagIds:['city'],jobTypes:['full_time'],remotePreference:'REMOTE_OPEN'};
        function run(){return fetch('/graphql',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({operationName:'JobSearchResultsX',variables:{filterConfigurationInput:cfg}})})}</script>'''
        def route(r):
            if r.request.url.endswith('/graphql'):
                self.requests.append(r.request.post_data_json)
                r.fulfill(content_type='application/json', body=json.dumps(payload()))
            else:
                r.fulfill(content_type='text/html', body=self.html)
        self.ctx.route('**/*', route)

    def test_native_controls_send_exact_phrase_without_inherited_role_city_or_job_type(self):
        search = native.NativeSearch(self.page, apply)
        search.start()
        jobs, more = search.search('Senior Ruby on Rails Developer')
        self.assertEqual(len(jobs), 1)
        config = self.requests[-1]['variables']['filterConfigurationInput']
        self.assertEqual(config, {'page': 1, 'keywords': ['Senior Ruby on Rails Developer'], 'remotePreference': 'REMOTE_ONLY'})
        self.assertFalse(more)

    def test_search_tab_blocks_save_apply_and_unknown_graphql_writes(self):
        search = native.NativeSearch(self.page, apply)
        search.start()
        for operation in ['Apply', 'SavedSearchCreate', 'ProfileUpdate']:
            result = self.page.evaluate("""async op=>{try {await fetch('/graphql',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({operationName:op})});return false} catch(e){return true}}""", operation)
            self.assertTrue(result)
        self.assertEqual(self.requests, [])
        spoofed = self.page.evaluate("""async ()=>{try {await fetch('/graphql',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({operationName:'JobSearchResultsX',query:'mutation JobSearchResultsX { changeProfile }'})});return false} catch(e){return true}}""")
        self.assertTrue(spoofed)

    def test_native_cached_response_requires_same_configuration_and_current_selection(self):
        search = native.NativeSearch(self.page, apply)
        search.start()
        keyword = 'Senior Ruby on Rails Developer'
        search.search(keyword)
        count = len(self.requests)
        self.page.evaluate('window.run=()=>Promise.resolve()')
        jobs, _ = search.search(keyword)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(len(self.requests), count)

    def test_keyword_must_also_appear_in_the_selected_ui_tokens(self):
        search = native.NativeSearch(self.page, apply)
        search.start()
        self.page.locator(native.KEYWORD_INPUT).evaluate("e=>e.onkeydown=()=>{document.getElementById('tokens').textContent='Different keyword'}")
        with self.assertRaises(native.SearchUnavailable):
            search.search('Senior Ruby on Rails Developer')
