"""Wellfound-only native keyword search; no guessed role taxonomy or API replay.

Uses the signed-in website's controls. Only its observed read-query operations
are allowed on the dedicated search tab; application/profile/save mutations are
not permitted here. Other tabs, providers and shared GUI filters are untouched.
"""
import copy
import re
from urllib.parse import urlparse

READ_OPERATIONS = frozenset({'JobSearchPage', 'JobSearchResultsX',
                             'ProfileCompletenessWidget', 'OptInModalAutoLauncher'})
KEYWORD_INPUT = '[data-test="KeywordsFilterField--keywords--input"]'


class SearchUnavailable(RuntimeError):
    pass


def verified_configuration(config: dict, keyword: str, page_number: int, remote_only: bool, company_regions=()) -> bool:
    """Reject query mismatches and undisclosed restrictions; region IDs are observed, never guessed."""
    if not isinstance(config, dict) or config.get('keywords') != [keyword] or config.get('page') != page_number:
        return False
    if config.get('remotePreference') != ('REMOTE_ONLY' if remote_only else 'REMOTE_OPEN'):
        return False
    for key, value in config.items():
        if key in ('keywords', 'page', 'remotePreference'):
            continue
        if key == 'remoteCompanyLocationTagIds' and value == list(company_regions):
            continue
        if isinstance(value, dict):
            if any(v is not None for v in value.values()):
                return False
        elif value not in (None, False, [], ''):
            return False
    return True


def result_jobs(payload: dict, keyword: str, page_number: int) -> tuple[list[dict], bool]:
    """Read only matched listings from the native results connection, not HTML links."""
    try:
        if not isinstance(payload, dict):
            raise ValueError('invalid response')
        if payload.get('errors'):
            raise ValueError('provider query errors')
        result = payload['data']['talent']['jobSearchResults']
        edges = result['startups']['edges']
        if not isinstance(edges, list) or type(result['hasNextPage']) is not bool:
            raise ValueError('invalid results connection')
        jobs = []
        for edge in edges:
            company = edge['node']
            for listing in company['highlightedJobListings']:
                job_id, slug = str(listing['id']), listing['slug']
                if not re.fullmatch(r'\d+', job_id) or not isinstance(slug, str) or not re.fullmatch(r'[\w-]+', slug):
                    raise ValueError('invalid job identity')
                if not isinstance(listing['title'], str) or not listing['title'].strip() or not isinstance(company['name'], str) or not company['name'].strip():
                    raise ValueError('missing title/company')
                if type(listing.get('currentUserApplied')) is not bool:
                    raise ValueError('missing provider application state')
                description = listing.get('description') or ''
                if not isinstance(description, str) or len(description) > 1_000_000:
                    raise ValueError('invalid description')
                remote = listing.get('remoteConfig') or {}
                if not isinstance(remote, dict):
                    raise ValueError('invalid remote configuration')
                locations = listing.get('acceptedRemoteLocationNames') or listing.get('locationNames') or []
                if not isinstance(locations, list) or any(not isinstance(v, str) for v in locations):
                    raise ValueError('invalid locations')
                jobs.append({'id': f'wellfound:{job_id}-{slug}', 'title': listing['title'],
                             'company': company['name'], 'url': f'https://wellfound.com/jobs/{job_id}-{slug}',
                             'desc': description, 'location': ', '.join(locations),
                             'provider_applied': listing['currentUserApplied'],
                             'native_posted_at': listing.get('liveStartAt'),
                             'native_workplace': remote.get('kind'),
                             'native_remote': listing.get('remote'),
                             'search_provenance': {'mode': 'native_keywords', 'keyword': keyword,
                                                   'page': page_number, 'regions_applied': False}})
        return jobs, result['hasNextPage']
    except (KeyError, TypeError, ValueError) as e:
        raise SearchUnavailable('Wellfound returned an unrecognized or unsuccessful native search response') from e


class NativeSearch:
    def __init__(self, page, core):
        self.page, self.core = page, core
        self.responses = []
        self.blocked_operations = []
        self.initialized = False
        self.remote_only = core.CFG['search'].get('remote_only', False)
        self.company_regions = None
        self.company_region_label = ''

    def _request(self, request):
        if request.method != 'POST' or request.url != 'https://wellfound.com/graphql':
            return
        try:
            body = request.post_data_json
            if body.get('operationName') == 'JobSearchResultsX':
                regions = body['variables']['filterConfigurationInput'].get('remoteCompanyLocationTagIds') or []
                if self.company_regions is None and isinstance(regions, list) and all(isinstance(v, str) for v in regions):
                    self.company_regions = list(regions)
        except Exception:
            pass

    def _route(self, route):
        request = route.request
        if request.method in ('GET', 'HEAD', 'OPTIONS'):
            route.fallback()
            return
        try:
            data = request.post_data_json
        except Exception:
            data = None
        document = data.get('query') if isinstance(data, dict) else None
        read_document = document is None or (isinstance(document, str) and re.match(r'^\s*(?:query\b|\{)', document))
        if (request.url == 'https://wellfound.com/graphql' and isinstance(data, dict)
                and data.get('operationName') in READ_OPERATIONS and read_document):
            route.fallback()
        else:
            if request.url == 'https://wellfound.com/graphql' and isinstance(data, dict):
                self.blocked_operations.append(data.get('operationName'))
            route.abort()

    def _response(self, response):
        request = response.request
        if request.url != 'https://wellfound.com/graphql' or request.method != 'POST':
            return
        try:
            body = request.post_data_json
            if body.get('operationName') != 'JobSearchResultsX':
                return
            config = body['variables']['filterConfigurationInput']
            payload = response.json() if response.status == 200 else {'errors': ['HTTP failure']}
            self.responses.append((copy.deepcopy(config), payload))
            self.responses = self.responses[-32:]
        except Exception:
            # A malformed response cannot establish that a search was applied.
            pass

    def start(self):
        self.page.route('**/*', self._route)
        self.page.on('request', self._request)
        self.page.on('response', self._response)
        self.page.goto('https://wellfound.com/jobs', wait_until='domcontentloaded', timeout=30000)
        if urlparse(self.page.url).path.rstrip('/') != '/jobs' or self.core.blocked(self.page):
            raise SearchUnavailable('Sign in to Wellfound to use full GUI job titles in native keyword search')
        try:
            self.page.locator('[data-test="SearchBar-RoleSelect-FocusButton"]').first.wait_for(state='visible', timeout=10000)
        except Exception as e:
            raise SearchUnavailable('Wellfound native search controls are unavailable; login or a site update may be required') from e

    def _click(self, locator):
        self.core.click_form_control(locator)

    def _clear_tags(self, button, removals):
        self._click(button)
        self.core.pause(0.3, 0.3)
        for _ in range(50):
            tags = self.page.locator(removals + ':visible')
            if not tags.count():
                break
            self._click(tags.first)
            self.core.pause(0.2, 0.2)
        else:
            raise SearchUnavailable('Too many inherited Wellfound search tags')
        self._click(self.page.locator('h1').first)

    def _remote_menu(self, choice):
        self._click(self.page.locator('[role="combobox"][aria-labelledby="RemotePrefereceSelectField-label"] button').first)
        menu = self.page.locator('[role="listbox"][aria-labelledby="RemotePrefereceSelectField-label"]')
        self._click(menu.locator(f'label[for="{choice}"]'))
        self.core.pause(0.3, 0.3)
        if not menu.is_visible():
            self._click(self.page.locator('[role="combobox"][aria-labelledby="RemotePrefereceSelectField-label"] button').first)
            self.core.pause(0.2, 0.2)
        if not menu.get_by_label(choice, exact=True).is_checked():
            raise SearchUnavailable('Wellfound remote preference was not confirmed')
        return menu

    def _prepare(self):
        # Office-location controls disappear in remote-only mode. Expose them
        # temporarily so hidden inherited city restrictions can be removed.
        self._remote_menu('Include remote jobs')
        self._click(self.page.locator('h1').first)
        self._clear_tags(self.page.locator('[data-test="SearchBar-RoleSelect-FocusButton"]').first,
                         '[data-test="RoleSelectWrapper"] .select__multi-value__remove')
        self._clear_tags(self.page.locator('[data-test="SearchBar"] button[class*="locationField"]').first,
                         '[class*="locationWrapper"] .select__multi-value__remove')
        menu = self._remote_menu('Only remote jobs' if self.remote_only else 'Include remote jobs')
        self.company_region_label = menu.locator('button').first.inner_text().strip()
        self._click(self.page.locator('h1').first)
        self.initialized = True

    def search(self, keyword: str):
        try:
            if not self.initialized:
                self._prepare()
            # Clear additional search chips, including old keywords, using
            # their actual controls. The company-region preference remains a
            # separately disclosed Wellfound setting; never replace it with
            # an inferred country/continent or claim it came from the GUI.
            for _ in range(50):
                clear = self.page.locator('[data-test^="ActiveFilterSummary-FilterButton--"]:visible')
                if not clear.count():
                    break
                self._click(clear.first)
                self.core.pause(0.2, 0.2)
            else:
                raise SearchUnavailable('Too many inherited Wellfound search filters')
            self._click(self.page.get_by_role('button', name='Filters', exact=True))
            panel = self.page.locator('[role="dialog"][aria-modal="true"]').filter(has=self.page.locator(KEYWORD_INPUT))
            panel.wait_for(state='visible', timeout=6000)
            field = panel.locator(KEYWORD_INPUT)
            # Keep responses for identical configurations: the native client
            # may serve a repeated keyword from its own cache without another
            # HTTP request. Acceptance still requires the exact full config.
            field.fill(keyword)
            field.press('Enter')
            self.core.pause(0.3, 0.3)
            selection = panel.locator('[data-test="KeywordsFilterField--keywords"]')
            selected = [line.strip() for line in selection.inner_text().splitlines()
                        if line.strip() and line.strip() not in ('Included keywords', 'Clear')]
            if selected != [keyword]:
                raise SearchUnavailable('Wellfound did not select the exact single GUI keyword')
            self._click(panel.locator('[data-test="SearchBar-ViewResultsButton"]'))
            for _ in range(120):
                if self.core.STOP.is_set():
                    raise self.core.Stopped
                self.page.wait_for_timeout(250)
                for config, payload in reversed(self.responses):
                    if verified_configuration(config, keyword, 1, self.remote_only, self.company_regions or ()):
                        return result_jobs(payload, keyword, 1)
                if self.core.blocked(self.page):
                    raise SearchUnavailable('Wellfound requires user verification')
            raise SearchUnavailable('No successful Wellfound response matched the exact GUI keyword and disclosed filter scope')
        except SearchUnavailable:
            raise
        except Exception as e:
            if isinstance(e, self.core.Stopped):
                raise
            if self.core.browser_closed(e):
                raise self.core.BrowserClosed() from None
            raise SearchUnavailable('Could not safely configure Wellfound native keyword search') from e

    def close(self):
        try:
            self.page.close()
        except Exception as e:
            if not self.core.browser_closed(e):
                raise
