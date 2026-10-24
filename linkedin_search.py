"""LinkedIn-only search verification and bounded legacy/SDUI result collection."""
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse
import re
import time


class SearchUnavailable(RuntimeError):
    pass


class UnsupportedLocation(SearchUnavailable):
    pass


EXPERIENCE_LEVELS = ('Entry-level', 'Senior', 'Manager', 'Director', 'Executive')
LEGACY_LEVELS = {1: 'Internship', 2: 'Entry-level', 3: 'Associate', 4: 'Senior', 5: 'Director', 6: 'Executive'}


def desired_experience(core):
    selected = core.SETTINGS.get('linkedin_experience_levels')
    if selected is None:
        # Unsupported legacy categories must fail explicitly, never be substituted.
        return [LEGACY_LEVELS.get(value, str(value)) for value in core.CFG['search'].get('experience_levels', [])]
    if not isinstance(selected, list) or any(value not in EXPERIENCE_LEVELS for value in selected) or len(set(selected)) != len(selected):
        raise SearchUnavailable('Invalid LinkedIn experience levels; review your selected levels')
    return selected


COLLECT_JS = r"""async () => {
    const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
    const visible = e => e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden';
    const roots = () => [...document.querySelectorAll('[componentkey="SearchResultsMainContent"], .jobs-search-results-list, .jobs-search-results__list, .scaffold-layout__list')].filter(visible);
    const empty = () => /\b0 results\b|no matching jobs|no results found/i.test([...document.querySelectorAll('[componentkey="searchResultsHeaderComponent"], .jobs-search-results-list__title-heading, .jobs-search-results-list__subtitle, .jobs-search-no-results-banner')].filter(visible).map(e=>e.innerText).join('\n'));
    const ids = new Set();
    let rootCount = 0, stable = 0;
    const scan = () => {
        const active = roots(); rootCount = active.length;
        for (const root of active) {
            for (const card of root.querySelectorAll('[componentkey^="job-card-component-ref-"], [data-occludable-job-id], [data-job-id]')) {
                if (!visible(card)) continue;
                const key = card.getAttribute('componentkey') || '';
                const native = key.match(/^job-card-component-ref-(\d+)$/);
                const legacy = (card.getAttribute('data-occludable-job-id') || card.getAttribute('data-job-id') || '').match(/^(?:urn:li:jobPosting:)?(\d+)$/);
                if (native || legacy) ids.add((native || legacy)[1]);
            }
            for (const link of root.querySelectorAll('a[href*="/jobs/view/"],a[href*="currentJobId="]')) {
                if (!visible(link)) continue;
                const url = new URL(link.href, location.href);
                if (url.origin !== location.origin) continue;
                const id = url.pathname.match(/^\/jobs\/view\/(\d+)\/?$/)?.[1] || url.searchParams.get('currentJobId');
                if (id && /^\d+$/.test(id)) ids.add(id);
            }
        }
        return active;
    };
    for (let attempt = 0; attempt < 12; attempt++) {
        const active = scan();
        if (active.length && (ids.size || empty())) break;
        await sleep(250);
    }
    for (let attempt = 0; attempt < 12 && rootCount; attempt++) {
        const before = ids.size;
        for (const root of roots()) {
            const cards = [...root.querySelectorAll('[componentkey^="job-card-component-ref-"], [data-occludable-job-id], [data-job-id], a[href*="/jobs/view/"]')].filter(visible);
            cards.at(-1)?.scrollIntoView({block: 'end'});
            for (let parent = root, depth = 0; parent && depth < 5; parent = parent.parentElement, depth++) {
                if (parent.scrollHeight > parent.clientHeight + 10 && /auto|scroll/.test(getComputedStyle(parent).overflowY)) {
                    parent.scrollTop += Math.max(parent.clientHeight * .8, 200); break;
                }
            }
        }
        await sleep(250); scan();
        stable = ids.size === before ? stable + 1 : 0;
        if (stable >= 3) break;
    }
    const active = roots();
    const next = active.flatMap(root => [...root.querySelectorAll('[data-testid="pagination-controls-next-button-visible"], button[aria-label="View next page"], button[aria-label="Next"]')]).find(visible);
    const native = active.some(root => root.getAttribute('componentkey') === 'SearchResultsMainContent');
    return {ids: [...ids], root_count: rootCount, native, empty_confirmed: empty(),
            has_next: next ? !next.disabled && next.getAttribute('aria-disabled') !== 'true' : (native ? false : null)};
}"""


def normalized(value):
    return ' '.join(value.split()).casefold()


def native_url(original: str, geo_id: str) -> str:
    """Use only a geo ID observed in the site's exact matching suggestion."""
    if not re.fullmatch(r'\d+', geo_id):
        raise ValueError('Invalid LinkedIn geo ID')
    parsed = urlparse(original)
    params = parse_qs(parsed.query)
    params.pop('location', None)
    params['geoId'] = [geo_id]
    return urlunparse((parsed.scheme, parsed.netloc, '/jobs/search-results/', '', urlencode(params, doseq=True), ''))


class LinkedInSearch:
    def __init__(self, worker_page, core):
        self.core = core
        self.page = worker_page.context.new_page()
        self.page.set_default_timeout(6000)
        self.regions = {}
        self.current = None
        self.last = None
        self.verified_experience = []
        self.verified_filters = {}

    def wait(self, milliseconds):
        if self.core.STOP.is_set():
            raise self.core.Stopped
        if time.time() > self.core.DEADLINE:
            raise self.core.LimitReached
        self.page.wait_for_timeout(milliseconds)

    def navigate(self, url):
        self.page.goto(url, wait_until='domcontentloaded', timeout=30000)
        self.wait(2000)
        parsed = urlparse(self.page.url)
        if parsed.hostname not in ('www.linkedin.com', 'linkedin.com') or any(part in parsed.path for part in ('/login', '/checkpoint', '/authwall', '/signup')):
            raise self.core.SessionLost
        if not self.page.locator('input[id*="jobs-search-box-location"]').count():
            try:
                self.page.locator('[componentkey="searchResultsHeaderComponent"] [aria-label^="Location "]:visible').first.wait_for(state='visible', timeout=8000)
            except Exception as e:
                raise SearchUnavailable('LinkedIn search header did not appear after hydration') from e

    def location_label(self):
        labels = self.page.locator('[componentkey="searchResultsHeaderComponent"] [aria-label^="Location "]:visible')
        if labels.count():
            return (labels.first.get_attribute('aria-label') or '')[len('Location '):].strip()
        legacy = self.page.locator('input[id*="jobs-search-box-location"]')
        return legacy.first.input_value().strip() if legacy.count() else ''

    def resolve_region(self, location):
        if location in self.regions:
            value = self.regions[location]
            if isinstance(value, Exception):
                raise value
            return value
        native_label = self.page.locator('[componentkey="searchResultsHeaderComponent"] [aria-label^="Location "]:visible')
        if not self.page.locator('input[id*="jobs-search-box-location"]').count():
            try:
                native_label.first.wait_for(state='visible', timeout=8000)
            except Exception as e:
                raise SearchUnavailable('LinkedIn location control did not appear after hydration') from e
        params = parse_qs(urlparse(self.page.url).query)
        current = params.get('geoId', [])
        if len(current) == 1 and re.fullmatch(r'\d+', current[0]) and normalized(self.location_label()) == normalized(location):
            self.regions[location] = current[0]
            return current[0]
        header = self.page.locator('[componentkey="searchResultsHeaderComponent"]')
        trigger = header.locator('[role="button"]:visible').filter(has=self.page.locator('[aria-label^="Location "]'))
        if trigger.count() != 1:
            raise SearchUnavailable('LinkedIn location control is unavailable; requested region cannot be confirmed')
        suggestions = []
        for attempt in range(2):
            if attempt:
                self.page.keyboard.press('Escape')
                self.wait(250)
            field = self.page.get_by_role('textbox', name='Location', exact=True)
            if not field.is_visible():
                self.core.click_form_control(trigger)
            field.wait_for(state='visible', timeout=8000)
            field.click()
            # Native editing retriggers autocomplete even if hydration left the
            # same contenteditable value and an empty/stale suggestion list.
            field.press('ControlOrMeta+A')
            field.press('Backspace')
            field.press_sequentially(location, delay=40)
            for _ in range(32):
                self.wait(250)
                suggestions = self.page.locator('#semanticSearchBoxLocationList a:visible').evaluate_all('es=>es.map(e=>({text:e.innerText,href:e.href}))')
                exact = [entry for entry in suggestions if normalized(entry['text']) == normalized(location)]
                if len(exact) == 1:
                    target = urlparse(exact[0]['href'])
                    ids = parse_qs(target.query).get('geoId', [])
                    if target.hostname in ('www.linkedin.com', 'linkedin.com') and target.path.rstrip('/') == '/jobs/search-results' and len(ids) == 1 and re.fullmatch(r'\d+', ids[0]):
                        self.regions[location] = ids[0]
                        return ids[0]
        options = ', '.join(entry['text'] for entry in suggestions[:5]) or 'none'
        if not suggestions:
            # An empty response is not proof that the region is unsupported.
            # Do not poison the run cache for every later keyword.
            raise SearchUnavailable(f'LinkedIn returned no location suggestions for {location!r} after two native attempts; no region substitution was made')
        error = UnsupportedLocation(f'No exact LinkedIn location option for {location!r}; suggested options: {options}. No region substitution was made.')
        self.regions[location] = error
        raise error

    def experience_panel(self):
        triggers = self.page.locator('[componentkey="JobsSearchFilters"] [aria-label^="Filter by Experience level"]').locator('xpath=..')
        visible = []
        for _ in range(32):
            visible = [control for control in triggers.all() if control.is_visible()]
            if len(visible) == 1:
                break
            self.wait(250)
        if len(visible) != 1:
            raise SearchUnavailable('LinkedIn experience filter cannot be identified uniquely')
        self.core.click_form_control(visible[0])
        show = self.page.locator('a:visible,button:visible').filter(has_text=re.compile(r'^Show results$'))
        show.wait_for(state='visible', timeout=8000)
        panel = show.locator('xpath=ancestor::*[.//input[@type="checkbox"]][1]')
        if panel.count() != 1:
            raise SearchUnavailable('LinkedIn experience options cannot be inspected')
        return panel, show

    def experience(self, *, configure=False):
        expected = desired_experience(self.core)
        if not expected and 'linkedin_experience_levels' not in self.core.SETTINGS:
            return  # No configured experience filter.
        panel, show = self.experience_panel()
        options = panel.locator('input[type="checkbox"][aria-label]')
        names = options.evaluate_all('es=>es.map(e=>e.getAttribute("aria-label"))')
        if len(names) != len(set(names)) or not set(expected) <= set(names):
            self.page.keyboard.press('Escape')
            raise SearchUnavailable(f'LinkedIn does not offer the exact requested experience levels {expected!r}; available: {names!r}. Review levels in the GUI')
        changed = False
        for name in names:
            # Locate by exact DOM attribute without interpolating untrusted text.
            control = panel.get_by_role('checkbox', name=name, exact=True, include_hidden=True)
            if control.is_checked() != (name in expected):
                if not configure:
                    self.page.keyboard.press('Escape')
                    raise SearchUnavailable('LinkedIn changed or ignored the requested experience levels')
                labels = control.locator('..').locator('label')
                if labels.count() != 1:
                    raise SearchUnavailable('LinkedIn experience checkbox label is ambiguous')
                self.core.click_form_control(labels)
                changed = True
        selected = [name for name in names if panel.get_by_role('checkbox', name=name, exact=True, include_hidden=True).is_checked()]
        if set(selected) != set(expected):
            raise SearchUnavailable('LinkedIn experience selection did not persist')
        if changed:
            self.core.click_form_control(show)
            self.wait(1500)
            self.experience()  # Reopen and verify the applied state, not just draft toggles.
        else:
            self.verified_experience = list(selected)
            self.page.keyboard.press('Escape')
            self.wait(250)

    def filter_checkbox(self, name):
        controls = self.page.locator(f'[componentkey="JobsSearchFilters"]:visible input[aria-label="Filter by {name}"]')
        for _ in range(32):
            # evaluate_all tolerates the brief zero-control hydration window;
            # is_checked on a stale locator instead times out mid-navigation.
            states = controls.evaluate_all('es=>es.map(e=>e.checked)')
            if len(states) == 1:
                return controls.first, states[0]
            self.wait(250)
        raise SearchUnavailable(f'LinkedIn {name} filter cannot be verified after hydration')

    def checkbox(self, name, expected, *, configure=True):
        if self.page.locator('input[id*="jobs-search-box-location"]').count() and not self.page.locator('[componentkey="JobsSearchFilters"]').count():
            return  # Legacy URL filters remain subject to final job metadata gates.
        control, state = self.filter_checkbox(name)
        if state != expected:
            if not configure:
                raise SearchUnavailable(f'LinkedIn {name} filter was changed or ignored')
            self.core.click_form_control(control.locator('..').locator('label'))
            for _ in range(20):
                self.wait(250)
                control, state = self.filter_checkbox(name)
                if state == expected:
                    break
        if state != expected:
            raise SearchUnavailable(f'LinkedIn {name} filter was not applied')
        self.verified_filters[name] = state

    def verify(self, keyword, location, geo_id):
        params = parse_qs(urlparse(self.page.url).query)
        if params.get('geoId') != [geo_id] or normalized(self.location_label()) != normalized(location):
            raise SearchUnavailable('LinkedIn changed or ignored the requested region; results will not be processed')
        if len(params.get('keywords', [])) != 1 or normalized(params['keywords'][0]) != normalized(keyword):
            raise SearchUnavailable('LinkedIn changed the requested keyword; results will not be processed')
        field = self.page.locator('input[componentkey="semanticSearchBox"], input[id*="jobs-search-box-keyword"]')
        if field.count() and normalized(field.first.input_value()) != normalized(keyword):
            raise SearchUnavailable('LinkedIn keyword control does not match the requested search')
        if params.get('f_TPR') != [f"r{self.core.CFG['search']['posted_within_hours'] * 3600}"]:
            raise SearchUnavailable('LinkedIn did not retain the requested publication window')
        self.checkbox('Remote', bool(self.core.CFG['search'].get('remote_only')), configure=False)
        self.checkbox('Easy Apply', not self.core.EXTERNAL, configure=False)

    def search_page(self, keyword, location, index=0):
        if index == 0:
            if location in self.regions and isinstance(self.regions[location], Exception):
                raise self.regions[location]
            original = self.core.search_url(keyword, location)
            if location not in self.regions:
                self.navigate(original)
            geo_id = self.resolve_region(location)
            self.navigate(native_url(original, geo_id))
            self.checkbox('Remote', bool(self.core.CFG['search'].get('remote_only')))
            self.checkbox('Easy Apply', not self.core.EXTERNAL)
            self.experience(configure=True)
            self.current = (keyword, location, geo_id, 0)
        else:
            if self.current is None or self.current[:2] != (keyword, location) or self.current[3] != index - 1:
                raise SearchUnavailable('LinkedIn pagination state does not match the query')
            geo_id = self.current[2]
            next_button = self.page.locator('[componentkey="SearchResultsMainContent"] [data-testid="pagination-controls-next-button-visible"]')
            if next_button.count() == 1 and next_button.is_enabled():
                self.core.click_form_control(next_button)
                target = self.page.locator(f'[componentkey="SearchResultsMainContent"] button[aria-label="Page {index + 1}"][aria-current="true"]')
                target.wait_for(state='attached', timeout=8000)
            else:
                parsed = urlparse(self.page.url)
                if parsed.path.rstrip('/') == '/jobs/search-results':
                    # Native experience/Remote filters live in opaque site-owned
                    # parameters. Keep them during fallback pagination.
                    params = parse_qs(parsed.query)
                    params.pop('currentJobId', None)
                    params['start'] = [str(index * 25)]
                    self.navigate(urlunparse((parsed.scheme, parsed.netloc, parsed.path, '', urlencode(params, doseq=True), '')))
                else:
                    self.navigate(native_url(self.core.search_url(keyword, location), geo_id) + f'&start={index * 25}')
            self.wait(1000)
            self.current = (keyword, location, geo_id, index)
        self.verify(keyword, location, geo_id)
        self.experience()
        result = self.page.evaluate(COLLECT_JS)
        self.verify(keyword, location, geo_id)
        self.experience()
        if not result.get('root_count'):
            raise SearchUnavailable('LinkedIn search-results container was not found after hydration')
        if not result['ids'] and not result.get('empty_confirmed'):
            raise SearchUnavailable('LinkedIn did not provide job cards or an explicit empty-results state after hydration')
        if index and self.last and result['ids'] == self.last['ids']:
            raise SearchUnavailable('LinkedIn repeated the previous result page; pagination stopped')
        self.last = result
        return result

    def close(self):
        try:
            self.page.close()
        except Exception as e:
            if not self.core.browser_closed(e):
                raise
