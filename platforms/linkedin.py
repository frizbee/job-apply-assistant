"""linkedin platform adapter; policy/sending guards are injected shared services."""
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

class LinkedInAdapter(PlatformAdapter):
    def linkedin_submission_signals(self, page: Page) -> set[str]:
        """Explicit confirmation lines only, not generic 'thanks' or job descriptions."""
        texts = page.locator("dialog[open]:visible, [role='dialog']:visible, .artdeco-toast-item:visible, [role='status']:visible").all_inner_texts()
        return {line.strip().casefold() for text in texts for line in text.splitlines() if self.core.LINKEDIN_SENT_RE.fullmatch(line.strip())}

    def wait_linkedin_submission(self, page: Page, modal: Locator, before: set[str], timeout_seconds: float=25, expected_url: str | None=None) -> str:
        deadline = time.monotonic() + timeout_seconds
        while True:
            if expected_url is not None and page.url != expected_url:
                return 'submission_unconfirmed'
            if self.core.linkedin_submission_signals(page) - before:
                return 'applied'
            if modal.locator("[aria-invalid='true']:visible, .artdeco-inline-feedback--error:visible, [data-test-form-element-error-messages]:visible").count():
                return 'submission_rejected'
            alerts = modal.locator("[role='alert']:visible").all_inner_texts()
            if any((re.search('error|failed|unable|could not|something went wrong|required|try again|hata|zorunlu', a, re.I) for a in alerts)):
                return 'submission_unconfirmed'
            if time.monotonic() >= deadline:
                return 'submission_unconfirmed'
            self.core.pause(0.5, 0.5)

    def submit_linkedin(self, page: Page, modal: Locator, primary: Locator, job_id: str, title: str, company: str, applied: dict | None=None) -> str:
        if self.core.STOP.is_set():
            raise self.core.Stopped
        expected_url = page.url
        before = self.core.linkedin_submission_signals(page)
        url = f'https://www.linkedin.com/jobs/view/{job_id}/'
        status = self.core.submit_application(page, primary, job_id, title, company, url, lambda: self.core.wait_linkedin_submission(page, modal, before, expected_url=expected_url), applied)
        if status == 'applied':
            try:
                page.keyboard.press('Escape')
            except Exception:
                pass
        return status

    def apply_to_job(self, page: Page, job_id: str, title: str='', company: str='', applied: dict | None=None) -> str:
        btn = page.get_by_role('button', name=self.core.EASY_APPLY_RE).first
        if not btn.count():
            if not re.fullmatch('[0-9]+', job_id):
                return 'no_easy_apply'
            draft = page.locator(f'[componentkey="jdp-continue-button-{job_id}"]')
            if draft.count() != 1:
                return 'no_easy_apply'
            href = urlparse(draft.get_attribute('href') or '')
            if href.scheme != 'https' or href.hostname != 'www.linkedin.com' or href.path.rstrip('/') != f'/jobs/view/{job_id}' or (draft.get_attribute('aria-disabled') == 'true'):
                return 'no_easy_apply'
            btn = draft
        try:
            self.core.click_form_control(btn)
        except self.core.UnsafeFormControl:
            self.core.record_manual(job_id, title, company, page.url, 'submit-capable Easy Apply opener')
            return 'manual_review'
        modal = page.locator(self.core.MODAL).first
        try:
            modal.wait_for(timeout=10000)
            modal.locator('footer button').first.wait_for(timeout=20000)
        except self.core.PWTimeout:
            self.core.screenshot(page, f'{job_id}_notloaded')
            self.core.discard(page)
            return 'modal_not_opened'
        for _ in range(15):
            self.core.pause(1, 2)
            unanswered = self.core.fill_step(page, modal)
            if self.core.require_manual_review(page, modal, unanswered, job_id, title, company):
                self.core.discard(page)
                return 'manual_review'
            follow = modal.locator('input#follow-company-checkbox:checked')
            if follow.count():
                try:
                    self.core.click_form_control(modal.locator("label[for='follow-company-checkbox']"))
                except self.core.UnsafeFormControl:
                    self.core.require_manual_review(page, modal, ['unsafe follow-company control'], job_id, title, company)
                    self.core.discard(page)
                    return 'manual_review'
            primary = modal.locator('footer button:visible').last
            if not primary.count():
                self.core.screenshot(page, f'{job_id}_nobutton')
                self.core.discard(page)
                return 'skipped_no_button'
            action = self.core.application_button_action(primary)
            if action == 'submit':
                if not primary.is_enabled():
                    if self.core.require_manual_review(page, modal, ['Final submit button is disabled; site validation is incomplete'], job_id, title, company):
                        self.core.discard(page)
                        return 'manual_review'
                    continue
                if self.core.DRY_RUN:
                    self.core.screenshot(page, f'{job_id}_dryrun')
                    self.core.discard(page)
                    return self.core.preview_application(job_id)
                return self.core.submit_linkedin(page, modal, primary, job_id, title, company, applied)
            if action != 'next':
                self.core.require_manual_review(page, modal, ['unrecognized application action'], job_id, title, company)
                self.core.discard(page)
                return 'manual_review'
            before = modal.inner_text()
            try:
                self.core.click_navigation(primary)
            except self.core.UnsafeFormControl:
                self.core.require_manual_review(page, modal, ['submit-capable navigation control'], job_id, title, company)
                self.core.discard(page)
                return 'manual_review'
            self.core.pause(1.5, 3)
            if self.core.has_errors(modal) or modal.inner_text() == before:
                unanswered = self.core.fill_step(page, modal)
                if self.core.require_manual_review(page, modal, unanswered, job_id, title, company):
                    self.core.discard(page)
                    return 'manual_review'
                if self.core.application_button_action(primary) != 'next':
                    continue
                try:
                    self.core.click_navigation(primary)
                except self.core.UnsafeFormControl:
                    self.core.require_manual_review(page, modal, ['submit-capable navigation control'], job_id, title, company)
                    self.core.discard(page)
                    return 'manual_review'
                self.core.pause(1.5, 3)
                if self.core.has_errors(modal) or modal.inner_text() == before:
                    issues = self.core.linkedin_navigation_issues(modal)
                    if self.core.require_manual_review(page, modal, issues, job_id, title, company):
                        self.core.discard(page)
                        return 'manual_review'
        self.core.screenshot(page, f'{job_id}_toolong')
        self.core.record_manual(job_id, title, company, page.url, 'LinkedIn step limit reached before preview; reopen for manual input')
        self.core.discard(page)
        return 'skipped_too_many_steps'

    def linkedin_navigation_issues(self, form: Locator) -> list[str]:
        issues = self.core.native_form_issues(form)
        messages = form.locator("[role='alert']:visible, .artdeco-inline-feedback--error:visible, [data-test-form-element-error-messages]:visible").all_inner_texts()
        issues += [message.strip()[:300] for message in messages if message.strip()][:5]
        return list(dict.fromkeys(issues)) or ['LinkedIn did not advance after Next; review the current step']

    def external_url(self, page: Page) -> str | None:
        """Follow LinkedIn's external Apply action and return the company-site URL."""
        btn = page.get_by_role('button', name=self.core.APPLY_BTN_RE).or_(page.get_by_role('link', name=self.core.APPLY_BTN_RE))
        return self.core.follow_apply(page, btn.filter(has_not_text=self.core.EASY_APPLY_RE).first)

    def search_url(self, keyword: str, location: str) -> str:
        s = self.core.CFG['search']
        params = {'keywords': keyword, 'location': location, 'f_TPR': f'r{s['posted_within_hours'] * 3600}', 'sortBy': 'DD'}
        if not self.core.EXTERNAL:
            params['f_AL'] = 'true'
        if s.get('remote_only'):
            params['f_WT'] = '2'
        if s.get('experience_levels'):
            params['f_E'] = ','.join(map(str, s['experience_levels']))
        return 'https://www.linkedin.com/jobs/search/?' + self.core.urlencode(params)

    def open_list(self, page: Page, url: str) -> list[str] | None:
        try:
            page.goto(url, wait_until='domcontentloaded')
        except Exception as e:
            if 'TOO_MANY_REDIRECTS' in str(e):
                raise self.core.SessionLost() from None
            if self.core.browser_closed(e):
                raise self.core.BrowserClosed() from None
            self.core.log(f'  ! could not open page: {str(e)[:100]}')
            return None
        self.core.pause(3, 5)
        return list(dict.fromkeys((f for f in page.evaluate(self.core.SCRAPE_IDS_JS) if f and f.isdigit())))

    def job_lists(self, page: Page):
        """Search only the regions selected by the user, not personalized feeds."""
        locations = self.core.selected_locations()
        search = self.core.linkedin_search.LinkedInSearch(page, self.core)
        unsupported = set()
        try:
            for kw in self.core.CFG['search']['keywords']:
                for loc in locations:
                    key = f'linkedin:{kw}:{loc}'
                    if loc in unsupported:
                        self.core.METRICS.mark('searches_failed', key)
                        continue
                    for index in range(self.core.CFG['search']['pages_per_search']):
                        try:
                            result = search.search_page(kw, loc, index)
                        except self.core.linkedin_search.SearchUnavailable as e:
                            self.core.METRICS.mark('searches_failed', key)
                            self.core.log(f'  ! LinkedIn search not applied: {kw!r} @ {loc!r}: {e}')
                            if isinstance(e, self.core.linkedin_search.UnsupportedLocation):
                                unsupported.add(loc)
                            break
                        self.core.METRICS.mark('searches_verified', key)
                        found = result['ids']
                        self.core.log(f"  '{kw}' @ {loc}, page {index + 1}: {len(found)} jobs (region verified)")
                        yield found
                        if result['has_next'] is False or (result['has_next'] is None and len(found) < 25):
                            break
        finally:
            search.close()

    def job_info(self, page: Page) -> tuple[str, str, str]:
        """Use the current job card, never an assumed tab-title order."""
        details = {}
        for attempt in range(5):
            details = page.evaluate(self.core.job_metadata.DETAILS_JS)
            description = details.get('description', '').strip()
            if description.casefold() in ('about the job', 'about this job'):
                details['description'] = ''
            if details.get('title') and details.get('company') and details.get('description'):
                break
            if attempt < 4:
                page.wait_for_timeout(1000)
        title, company, description = (details.get('title', ''), details.get('company', ''), details.get('description', ''))
        if not title or not company or (not description):
            structured = self.core.job_metadata.page_metadata(page, 'linkedin', page.url, attempts=1)
            title = title or structured.get('title', '')
            company = company or structured.get('company', '')
            description = description or html.unescape(re.sub('<[^>]+>', ' ', structured.get('description', '') or ''))
        return (title, company, description)

    def linkedin_login(self, page: Page) -> bool:
        page.goto('https://www.linkedin.com/feed/', wait_until='domcontentloaded')
        self.core.pause(2, 4)
        if re.search('login|checkpoint|authwall|signup|uas/', page.url) or page.url.rstrip('/') == 'https://www.linkedin.com':
            self.core.log('Not signed in. Please sign in to LinkedIn in the open Chrome window (waiting up to 10 minutes)...')
            if 'login' not in page.url:
                page.goto('https://www.linkedin.com/login', wait_until='domcontentloaded')
            try:
                page.wait_for_url(re.compile('linkedin\\.com/(feed|in/|mynetwork|jobs)'), timeout=600000)
            except self.core.PWTimeout:
                self.core.log('LinkedIn login was not completed; moving to other sources.')
                return False
            self.core.pause(3, 5)
        self.core.log('LinkedIn login complete.')
        return True

    def run_linkedin(self, page: Page, applied: dict, count: int) -> int:
        if self.core.submission_quota_status('linkedin') or count >= self.core.CFG['limits']['max_applications_per_run']:
            return count
        if not self.core.linkedin_login(page):
            return count
        stop = min(self.core.CFG['limits']['max_applications_per_run'], count + self.core.CFG['limits'].get('per_source', 15))
        visited = set()
        try:
            for job_ids in self.core.job_lists(page):
                for job_id in job_ids:
                    if job_id in visited:
                        continue
                    visited.add(job_id)
                    if not self.core.should_visit_job(applied, job_id):
                        continue
                    if count >= stop or self.core.submission_quota_status('linkedin') or time.time() > self.core.DEADLINE:
                        raise self.core.LimitReached
                    with self.core.submission_source('linkedin'):
                        if self.core.process_job(page, applied, job_id):
                            count += 1
        except self.core.LimitReached:
            self.core.log('Run application limit or runtime limit reached.')
        except self.core.SessionLost:
            self.core.log('LinkedIn session expired. Sign in again on the next run; moving to other sources.')
        return count
