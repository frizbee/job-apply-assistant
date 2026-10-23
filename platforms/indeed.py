"""indeed platform adapter; policy/sending guards are injected shared services."""
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

INDEED_AUTH_PATH = re.compile(r'/(?:auth|account|accounts|login|signin|sign-in|registration)(?:/|$)', re.I)

INDEED_AUTH_HEADING = re.compile(r'^\s*(?:sign\s*in|log\s*in|create (?:an? |your )?account|welcome back|verify (?:your )?email|enter (?:your )?(?:email|password|code)|two.step verification)\b', re.I)

class IndeedAdapter(PlatformAdapter):
    def __init__(self, services):
        super().__init__(services.core, services)

    def indeed_host(self, page) -> bool:
        host = (urlparse(page.url).hostname or '').lower()
        configured = self.core.CFG['sources'].get('indeed_domain', 'www.indeed.com').lower().removeprefix('www.')
        return bool(host) and (host == configured or host.endswith('.' + configured) or host == 'indeed.com' or host.endswith('.indeed.com'))

    def indeed_auth_pending(self, page) -> bool:
        parsed = urlparse(page.url)
        host = (parsed.hostname or '').lower()
        if host in ('accounts.indeed.com', 'account.indeed.com', 'auth.indeed.com') or self.services.INDEED_AUTH_PATH.search(parsed.path):
            return True
        if page.locator('input[type="password"]:visible, input[autocomplete="username"]:visible, form[action*="/login"]:visible').count():
            return True
        return bool(page.get_by_role('heading', name=self.services.INDEED_AUTH_HEADING).count())

    def indeed_ready_evidence(self, page, stage: str) -> bool:
        """Positive page evidence, not just the temporary absence of a challenge."""
        try:
            if not self.services.indeed_host(page) or self.core.blocked(page) or self.services.indeed_auth_pending(page):
                return False
            if stage == 'search':
                return bool(page.locator('[data-jk]:visible, .jobsearch-NoResult-messageContainer:visible, [data-testid="no-results"]:visible, #no_results:visible').count())
            if stage == 'detail':
                return bool(page.locator('#jobDescriptionText:visible, [data-testid="jobsearch-JobInfoHeader-title"]:visible, .jobsearch-JobInfoHeader-title:visible, #indeedApplyButton:visible').count())
            if stage == 'application':
                return bool(page.get_by_role('button', name=self.core.NAVIGATION_BUTTON_RE).count() or page.get_by_role('button', name=self.core.FINAL_BUTTON_RE).count())
            return False
        except Exception as e:
            if self.core.browser_closed(e):
                raise self.core.BrowserClosed() from None
            return False

    def indeed_wait_ready(self, page, stage: str) -> bool:
        """Wait passively through login/MFA/challenge; never reload or navigate here."""
        state = {'url': None, 'since': None}

        def stable(p):
            if self.core.STOP.is_set():
                raise self.core.Stopped
            if not self.services.indeed_ready_evidence(p, stage):
                state.update(url=None, since=None)
                return False
            now = time.monotonic()
            if state['url'] != p.url or state['since'] is None:
                state.update(url=p.url, since=now)
                return False
            return now - state['since'] >= 1
        if self.services.indeed_ready_evidence(page, stage):
            stable(page)
            page.wait_for_timeout(1100)
            if stable(page):
                return True
        return self.core.wait_human(page, stable, 'finish Indeed sign-in or security verification (the app will not reload the page)', minutes=10)

    def indeed_apply(self, page, job: dict, applied: dict | None=None) -> str:
        if not self.services.goto(page, job['url'], 'Indeed'):
            return 'blocked'
        easy = page.locator("#indeedApplyButton, button[id*='indeedApply']").first
        if not easy.count():
            btn = page.get_by_role('button', name=re.compile('apply on company site', re.I)).or_(page.get_by_role('link', name=re.compile('apply on company site', re.I)))
            return self.services.external(page, job, self.core.follow_apply(page, btn.first), 'indeed.com', applied)
        ctx, before = (page.context, len(page.context.pages))
        try:
            self.core.click_form_control(easy)
        except self.core.UnsafeFormControl:
            self.core.record_manual(job['id'], job['title'], job['company'], job['url'], 'submit-capable Indeed opener')
            return 'manual_review'
        self.core.pause(4, 6)
        ap = ctx.pages[-1] if len(ctx.pages) > before else page
        for _ in range(15):
            self.core.pause(1.5, 3)
            if not self.services.indeed_wait_ready(ap, 'application'):
                return 'blocked'
            scope = ap.locator('main').first if ap.locator('main').count() else ap.locator('body')
            unanswered = self.core.fill_step(ap, scope, external=True)
            if self.core.require_manual_review(ap, scope, unanswered, job['id'], job['title'], job['company'], job['url'], external=True):
                return 'manual_review'
            primary = ap.get_by_role('button', name=re.compile('^(next|continue|review|submit|send(?: your)? application|devam|[iİıI]leri|başvurunu gönder|gönder)', re.I)).last
            if not primary.count():
                self.core.screenshot(ap, f'{job['id'].replace(':', '_')}_nobutton')
                return 'skipped_no_button'
            action = self.core.application_button_action(primary)
            if action == 'submit':
                if self.core.DRY_RUN:
                    self.core.screenshot(ap, f'{job['id'].replace(':', '_')}_dryrun')
                    return self.core.preview_application(job['id'])
                before = self.core.application_submission_signals(ap)
                expected_host = urlparse(ap.url).hostname
                return self.core.submit_application(ap, primary, job['id'], job['title'], job['company'], job['url'], lambda: self.core.wait_application_submission(ap, before, checks=15, expected_host=expected_host), applied)
            if action != 'next':
                self.core.require_manual_review(ap, scope, ['unrecognized application action'], job['id'], job['title'], job['company'], job['url'])
                return 'manual_review'
            before_url, before_text = (ap.url, ap.locator('body').inner_text()[:3000])
            try:
                self.core.click_navigation(primary)
            except self.core.UnsafeFormControl:
                self.core.require_manual_review(ap, scope, ['submit-capable navigation control'], job['id'], job['title'], job['company'], job['url'])
                return 'manual_review'
            self.core.pause(2, 3)
            if ap.url == before_url and ap.locator('body').inner_text()[:3000] == before_text:
                self.core.screenshot(ap, f'{job['id'].replace(':', '_')}_unanswered')
                return 'skipped_unanswered'
        return 'skipped_too_many_steps'

    def indeed(self, page):
        cfg = self.core.CFG['sources']
        domain = cfg.get('indeed_domain', 'www.indeed.com')
        days = max(1, self.core.CFG['search']['posted_within_hours'] // 24)
        seen = set()
        for kw in self.core.CFG['search']['keywords']:
            url = f'https://{domain}/jobs?q={quote_plus(kw)}&l=Remote&fromage={days}&sort=date'
            if not self.services.goto(page, url, 'Indeed'):
                return
            cards = page.locator('[data-jk]').evaluate_all("els => els.map(e => [e.getAttribute('data-jk'), (e.closest('li, .job_seen_beacon, td') || e).innerText])")
            for jk, text in cards:
                if jk in seen:
                    continue
                seen.add(jk)
                lines = [l.strip() for l in text.splitlines() if l.strip()]
                title = re.sub('^new\\s+', '', lines[0], flags=re.I) if lines else ''
                company = lines[1] if len(lines) > 1 else ''
                if cfg.get('indeed_easy_apply_only', False) and (not re.search('easily apply', text, re.I)):
                    continue
                job = {'id': f'indeed:{jk}', 'title': title, 'company': company, 'url': f'https://{domain}/viewjob?jk={jk}', 'desc': text, 'location': ''}
                yield (job, lambda applied=None, job=job: self.services.indeed_apply(page, job, applied))
