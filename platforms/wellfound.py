"""wellfound platform adapter; policy/sending guards are injected shared services."""
import datetime as dt
import html
import json
import re
import time
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus, urlparse
from playwright.sync_api import Page, Locator
from .base import PlatformAdapter

WELLFOUND_OPENER_JS = r"""job => {
    document.querySelectorAll('[data-autoapply-wellfound-opener]').forEach(e => e.removeAttribute('data-autoapply-wellfound-opener'));
    if (location.origin + location.pathname.replace(/\/$/, '') !== job.url.replace(/\/$/, '')) return false;
    const clean = s => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
    const visible = e => e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden';
    const headings = [...document.querySelectorAll('h1')].filter(e => visible(e) && clean(e.innerText) === clean(job.title));
    if (headings.length !== 1) return false;
    for (let root = headings[0].parentElement, depth = 0; root && depth < 6; root = root.parentElement, depth++) {
        if (root.matches('body, html') || root.querySelectorAll('h1').length !== 1) break;
        const otherJob = [...root.querySelectorAll('a[href*="/jobs/"]')].some(e => {
            const u = new URL(e.href, location.href);
            return /\/jobs\/\d+/.test(u.pathname) && u.origin + u.pathname.replace(/\/$/, '') !== job.url.replace(/\/$/, '');
        });
        if (otherJob) break;
        const buttons = [...root.querySelectorAll('button')].filter(e => visible(e) && /^apply( now)?$/i.test(clean(e.innerText))
            && (!e.getAttribute('aria-label') || /^apply( now)?$/i.test(clean(e.getAttribute('aria-label')))));
        if (buttons.length === 1) { buttons[0].setAttribute('data-autoapply-wellfound-opener', 'true'); return true; }
        if (buttons.length > 1) return false;
    }
    return false;
}"""

WELLFOUND_SEARCH_JS = r"""() => {
    const input = document.querySelector('input[id^="react-select-"]');
    const control = input?.closest('.select__control');
    const value = control?.querySelector('.select__single-value');
    return value && value.getClientRects().length ? value.innerText.trim() : '';
}"""

class WellfoundAdapter(PlatformAdapter):
    def __init__(self, services):
        super().__init__(services.core, services)

    def wellfound_login(self, page) -> bool:
        if 'wellfound' not in self.services.LOGGED_IN:
            logged = lambda p: self.services.page_ready(p) and (not p.get_by_role('link', name=re.compile('^log ?in$', re.I)).count())
            page.goto('https://wellfound.com/jobs', wait_until='domcontentloaded')
            self.core.pause(2, 4)
            if not logged(page):
                page.goto('https://wellfound.com/login', wait_until='domcontentloaded')
            self.services.LOGGED_IN['wellfound'] = self.core.wait_human(page, lambda p: '/login' not in p.url and logged(p), 'sign in to Wellfound', minutes=10)
        return self.services.LOGGED_IN['wellfound']

    def wellfound_apply(self, page, job: dict, applied: dict | None=None) -> str:
        if not self.services.wellfound_login(page) or not self.services.goto(page, job['url'], 'Wellfound'):
            return 'blocked'
        ctx, before, start = (page.context, len(page.context.pages), page.url)
        if page.evaluate(self.services.WELLFOUND_OPENER_JS, {'url': job['url'], 'title': job['title']}) is not True:
            self.core.record_manual(job['id'], job['title'], job['company'], job['url'], 'Cannot uniquely associate Apply with the current job')
            return 'manual_review'
        btn = page.locator('[data-autoapply-wellfound-opener="true"]')
        try:
            self.core.click_form_control(btn)
        except self.core.UnsafeFormControl:
            self.core.record_manual(job['id'], job['title'], job['company'], job['url'], 'submit-capable Apply opener')
            return 'manual_review'
        self.core.pause(3, 4)
        if len(ctx.pages) > before or not page.url.startswith('https://wellfound.com'):
            new = ctx.pages[-1] if len(ctx.pages) > before else page
            url = new.url
            if new is not page:
                new.close()
            else:
                page.goto(start, wait_until='domcontentloaded')
            return self.services.external(page, job, url, 'wellfound.com', applied)
        forms = page.locator("dialog[open]:visible, div[role='dialog']:visible")
        if not forms.count():
            forms = page.locator('form:has(textarea):visible')
        if forms.count() != 1:
            self.core.record_manual(job['id'], job['title'], job['company'], job['url'], 'Application form missing or ambiguous after Apply')
            return 'manual_review'
        form = forms.first
        unanswered = self.core.fill_step(page, form, external=True)
        if self.core.require_manual_review(page, form, unanswered, job['id'], job['title'], job['company'], job['url'], external=True):
            return 'manual_review'
        sends = form.get_by_role('button', name=re.compile('send application|submit|^apply', re.I))
        if not sends.count():
            self.core.screenshot(page, f'{job['id'].replace(':', '_')}_unanswered')
            return 'skipped_unanswered'
        if sends.count() != 1:
            self.core.record_manual(job['id'], job['title'], job['company'], job['url'], 'Ambiguous final application action')
            return 'manual_review'
        send = sends.first
        if self.core.application_button_action(send) != 'submit':
            self.core.record_manual(job['id'], job['title'], job['company'], job['url'], 'Unrecognized final application action')
            return 'manual_review'
        if self.core.DRY_RUN:
            self.core.screenshot(page, f'{job['id'].replace(':', '_')}_dryrun')
            page.keyboard.press('Escape')
            return self.core.preview_application(job['id'])
        before = self.core.application_submission_signals(page)
        expected_host = urlparse(page.url).hostname
        return self.core.submit_application(page, send, job['id'], job['title'], job['company'], job['url'], lambda: self.core.wait_application_submission(page, before, checks=15, expected_host=expected_host), applied)

    def wellfound_search_verified(self, page, requested_url: str, keyword: str) -> bool:
        """Never treat a generic /remote redirect or an unselected role as a query."""
        expected, actual = (urlparse(requested_url), urlparse(page.url))
        if (actual.scheme, actual.netloc, actual.path.rstrip('/')) != (expected.scheme, expected.netloc, expected.path.rstrip('/')):
            return False
        for attempt in range(3):
            selected = page.evaluate(self.services.WELLFOUND_SEARCH_JS)
            if isinstance(selected, str) and ' '.join(selected.split()).casefold() == ' '.join(keyword.split()).casefold():
                return True
            if attempt < 2:
                page.wait_for_timeout(500)
        return False

    def _wellfound_public(self, page):
        seen = set()
        for kw in self.core.CFG['search']['keywords']:
            search_url = f'https://wellfound.com/role/r/{self.services.slug(kw)}'
            if not self.services.goto(page, search_url, 'Wellfound'):
                self.core.METRICS.mark('searches_failed', f'wellfound:{kw}')
                return
            if not self.services.wellfound_search_verified(page, search_url, kw):
                self.core.METRICS.mark('searches_failed', f'wellfound:{kw}')
                self.core.log(f'  ! Wellfound search not applied for {kw!r}: requested {search_url}, actual {page.url.split('?')[0]}. The exact role was not confirmed; generic results will not be processed. Choose a supported role explicitly in the GUI; no replacement was made.')
                continue
            self.core.METRICS.mark('searches_verified', f'wellfound:{kw}')
            ids = dict.fromkeys(re.findall('/jobs/(\\d+-[\\w-]+)', page.content()))
            for jid in ids:
                if jid in seen:
                    continue
                seen.add(jid)
                url = f'https://wellfound.com/jobs/{jid}'
                prev = self.core.load_applied().get(f'wellfound:{jid}')
                if prev and (not self.core.can_retry(prev['status'])) and (prev['status'] not in self.core.job_rechecks.RECHECKABLE):
                    self.core.METRICS.mark('seen', f'wellfound:{jid}')
                    self.core.METRICS.mark('history_skipped', f'wellfound:{jid}')
                    continue
                if not self.services.goto(page, url, 'Wellfound'):
                    return
                m = re.match('(.+?) at (.+?)(?: •|$)', page.title())
                title, company = (m.group(1), m.group(2)) if m else (page.title(), '')
                metadata = self.core.read_job_metadata(page, 'wellfound', url)
                description = metadata.get('description') or ''
                if description:
                    description = html.unescape(re.sub('<[^>]+>', '\n', description))
                else:
                    scoped = page.locator('[data-testid="job-description"], [data-test="job-description"], #job-description, .job-description')
                    description = scoped.first.inner_text() if scoped.count() else ''
                job = {'id': f'wellfound:{jid}', 'title': title, 'company': company, 'url': url, 'desc': description, 'location': '', 'metadata': metadata}
                yield (job, lambda applied=None, job=job: self.services.wellfound_apply(page, job, applied))

    def wellfound(self, page):
        """Use native literal keyword search when signed in; preserve strict public fallback."""
        from wellfound_search import NativeSearch, SearchUnavailable
        search_page = page.context.new_page()
        search_page.set_default_timeout(6000)
        search = NativeSearch(search_page, self.core)
        try:
            try:
                search.start()
            except SearchUnavailable as e:
                self.core.log(f'  ! Wellfound native keyword search unavailable: {e}. Trying only verifiable exact public roles.')
                yield from self.services._wellfound_public(page)
                return
            seen = set()
            for keyword in list(self.core.CFG['search']['keywords']):
                if self.core.STOP.is_set():
                    raise self.core.Stopped
                if time.time() > self.core.DEADLINE:
                    return
                try:
                    jobs, more = search.search(keyword)
                except SearchUnavailable as e:
                    self.core.METRICS.mark('searches_failed', f'wellfound:{keyword}')
                    self.core.log(f'  ! Wellfound native search failed for {keyword!r}: {e}')
                    continue
                self.core.METRICS.mark('searches_verified', f'wellfound:{keyword}')
                self.services.LOGGED_IN['wellfound'] = True
                self.core.log(f'  Wellfound native keyword {keyword!r}: {len(jobs)} job listings. Site company-region scope: {search.company_region_label or 'not specified'}. GUI regions are not mapped here; Wellfound may additionally limit results by account location.')
                if more:
                    self.core.log('  ! Wellfound has more native results; only the first page is currently supported.')
                for job in jobs:
                    if job['id'] in seen:
                        continue
                    seen.add(job['id'])
                    self.core.METRICS.mark('seen', job['id'])
                    history = self.core.load_applied()
                    identity_prefix = job['id'].split('-', 1)[0]
                    previous = [value for key, value in history.items() if key == identity_prefix or key.startswith(identity_prefix + '-')]
                    protected = any((not self.core.can_retry(value['status']) and value['status'] not in self.core.job_rechecks.RECHECKABLE for value in previous))
                    if job.pop('provider_applied') or protected:
                        self.core.METRICS.mark('history_skipped', job['id'])
                        continue
                    workplace = job.pop('native_workplace')
                    job['metadata'] = {'posted_at': job.pop('native_posted_at'), 'workplace': self.services.job_metadata.workplace_kind(workplace.replace('_', ' ') if isinstance(workplace, str) else None, job.pop('native_remote')), 'location': job['location'], 'diagnostic': {'source': 'wellfound_native_results', 'keyword': keyword, 'page': 1, 'company_region_scope': search.company_region_label, 'gui_regions_applied': False}}
                    yield (job, lambda applied=None, job=job: self.services.wellfound_apply(page, job, applied))
        finally:
            search.close()
