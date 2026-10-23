"""ats platform adapter; policy/sending guards are injected shared services."""
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
from platforms.base import PlatformAdapter

class ATSEngine(PlatformAdapter):
    def application_submission_signals(self, page: Page) -> set[str]:
        texts = page.locator("[role='dialog']:visible, [role='status']:visible, [role='alert']:visible, main h1:visible, main h2:visible, [data-testid='application-confirmation']:visible").all_inner_texts()
        return {line.strip().casefold() for text in texts for line in text.splitlines() if self.core.APPLICATION_SENT_RE.fullmatch(line.strip())}

    def wait_application_submission(self, page: Page, before: set[str], checks: int=25, expected_host: str | None=None) -> str:
        for _ in range(checks):
            if expected_host is not None and urlparse(page.url).hostname != expected_host:
                return 'submission_unconfirmed'
            if self.core.application_submission_signals(page) - before:
                return 'applied'
            if page.locator("[aria-invalid='true']:visible, [class*='error' i]:visible:has-text('required')").count():
                return 'submission_rejected'
            if page.locator(self.core.CAPTCHA_SEL).count():
                return 'submission_unconfirmed'
            self.core.pause(1, 1)
        return 'submission_unconfirmed'

    def ensure_application_scope(self, scope: Locator, field: Locator | None=None, *, question: str='') -> None:
        if scope.count() != 1 or (field is not None and field.count() != 1):
            raise self.core.ApplicationFormChanged('application_form_changed_during_fill' + (': ' + question[:120] if question else ''))

    def stable_application_controls(self, scope: Locator, selector: str):
        """Bind controls by unique native IDs, or by per-node tokens as a fallback."""
        token = self.core.secrets.token_hex(12)
        script = '(es, token) => es.map((e,i) => {\n        let selector;\n        if (e.id && document.querySelectorAll(\'#\'+CSS.escape(e.id)).length === 1) {\n            selector = \'#\'+CSS.escape(e.id);\n        } else {\n            e.setAttribute(\'data-autoapply-field\', token+\'-\'+i);\n            selector = \'[data-autoapply-field="\'+token+\'-\'+i+\'"]\';\n        }\n        return {selector, question: (' + self.core.QUESTION_JS + ')(e)};\n    })'
        return [(scope.locator(entry['selector']), entry['question']) for entry in scope.locator(selector).evaluate_all(script, token)]

    def trusted_ats_document(self, page: Page, ats: str) -> Locator:
        """Bind all data-entry locators to one verified document, not future navigations.

        Checking page.url alone is racy: Playwright can wait for an element on a new,
        untrusted page. A random document marker prevents that selector from matching.
        """
        url = page.url
        if ats not in self.core.ATS or self.core.ats_provider(url) != ats:
            raise self.core.UntrustedApplicationSite('untrusted_redirect')
        token = self.core.secrets.token_hex(16)
        try:
            marked = page.evaluate("""({url, token, identity}) => {
                if (window.location.href !== url) return false;
                document.documentElement.setAttribute('data-autoapply-document', token);
                if (identity) document.documentElement.setAttribute('data-autoapply-application-key', identity);
                else document.documentElement.removeAttribute('data-autoapply-application-key');
                return true;
            }""", {'url': url, 'token': token,
                    'identity': self.core.application_identity.application_key(url, self.core)})
        except Exception as e:
            if self.core.browser_closed(e):
                raise self.core.BrowserClosed() from None
            raise self.core.UntrustedApplicationSite('cannot_verify_document') from e
        if marked is not True:
            raise self.core.UntrustedApplicationSite('document_changed')
        return page.locator(f'html[data-autoapply-document="{token}"]')

    def check_ats_form(self, page: Page, document: Locator, form: Locator, ats: str) -> None:
        if ats not in self.core.ATS or self.core.ats_provider(page.url) != ats or document.count() != 1:
            raise self.core.UntrustedApplicationSite('document_changed_or_untrusted_redirect')
        identity = document.get_attribute('data-autoapply-application-key')
        if isinstance(identity, str) and identity and identity != self.core.application_identity.application_key(page.url, self.core):
            raise self.core.UntrustedApplicationSite('ats_job_identity_changed')
        try:
            targets = form.evaluate("root => {\n            const controls = [...root.querySelectorAll('input, button, select, textarea')];\n            const forms = new Set(root.matches('form') ? [root] : root.querySelectorAll('form'));\n            for (const el of controls) if (el.form) forms.add(el.form);\n            return [...forms].map(f => f.action || window.location.href).concat(\n                controls.filter(e => e.hasAttribute('formaction')).map(e => e.formAction));\n        }")
        except Exception as e:
            if self.core.browser_closed(e):
                raise self.core.BrowserClosed() from None
            raise self.core.UntrustedApplicationSite('cannot_verify_form_targets') from e
        if any((self.core.ats_provider(target) != ats for target in targets)):
            raise self.core.UntrustedApplicationSite('untrusted_form_target')

    def follow_apply(self, page: Page, btn: Locator) -> str | None:
        """Click Apply and return the destination URL from navigation or a new tab."""
        if not btn.count():
            return None
        ctx = page.context
        before, start = (len(ctx.pages), page.url)
        try:
            self.core.click_form_control(btn)
        except self.core.UnsafeFormControl:
            return None
        for _ in range(30):
            time.sleep(0.5)
            if page.url != start and urlparse(page.url).netloc != urlparse(start).netloc:
                url = page.url
                page.go_back(wait_until='domcontentloaded')
                return url
            if len(ctx.pages) > before:
                new = ctx.pages[-1]
                try:
                    new.wait_for_load_state('domcontentloaded', timeout=20000)
                    self.core.pause(2, 3)
                except self.core.PWTimeout:
                    pass
                url = new.url
                new.close()
                return url
            cont = page.locator(self.core.MODAL).get_by_role('button', name=re.compile('continue|devam', re.I))
            if cont.count():
                try:
                    self.core.click_form_control(cont.first)
                except self.core.UnsafeFormControl:
                    return None
        return None

    def ats_apply_url(self, page: Page, url: str) -> tuple[str | None, str]:
        """Find a direct application URL/provider, including embedded company-site forms."""
        name = self.core.ats_provider(url)
        if not name:
            if not self.core.safe_https_url(url):
                return (None, 'unknown')
            try:
                page.goto(url, wait_until='domcontentloaded', timeout=30000)
                self.core.pause(3, 4)
            except Exception:
                return (None, 'unknown')
            if not self.core.safe_https_url(page.url):
                return (None, 'unknown')
            name = self.core.ats_provider(page.url)
            if name:
                url = page.url
            else:
                for fr in page.frames:
                    name = self.core.ats_provider(fr.url)
                    if name:
                        url = fr.url
                        break
                else:
                    return (None, 'unknown')
        return (self.core.ats_adapter(name, self.core).application_url(url), name)

    def apply_form(self, page: Page, job_id: str, title: str, company: str, url: str, applied: dict | None=None) -> str:
        """Process a company-site form with a per-run quota and inter-attempt cooldown."""
        ext = self.core.CFG['external']
        if not self.core.EXTERNAL:
            self.core.record_manual(job_id, title, company, url, 'external_disabled')
            return 'external_manual'
        core = self.core
        history = applied if applied is not None else self.core.load_applied()
        ledger = self.core.application_identity.ledger(core)
        identity = self.core.application_identity.application_key(url, core)
        if identity:
            cached = ledger.begin(identity, job_id, url, history, core)
            if cached:
                self.core.log(f'  = {cached}: final ATS job was already processed/queued; no form/quota consumed')
                return cached
        denied = self.core.submission_quota_status(self.core.source_for_job(job_id))
        if denied:
            ledger.abandon(identity, job_id)
            return denied
        if self.core.EXT_STATE['count'] >= ext.get('max_per_run', 5):
            ledger.abandon(identity, job_id)
            return 'skipped_external_limit'
        form_url, ats = self.core.ats_apply_url(page, url)
        if not form_url:
            self.core.record_manual(job_id, title, company, url, 'unsupported_site')
            return 'external_manual'
        resolved = self.core.application_identity.application_key(form_url, core)
        if resolved != identity:
            ledger.abandon(identity, job_id)
            identity = resolved
            if identity:
                cached = ledger.begin(identity, job_id, form_url, history, core)
                if cached:
                    self.core.log(f'  = {cached}: duplicate final ATS job; no form/quota consumed')
                    return cached
        wait = self.core.EXT_STATE['last'] + ext.get('min_wait_seconds', 120) - time.time()
        if wait > 0:
            self.core.log(f'  .. waiting {int(wait)} seconds before the next company form')
            self.core.pause(wait, wait)
        self.core.EXT_STATE['count'] += 1
        self.core.EXT_STATE['last'] = time.time()
        self.core.log(f'  -> {ats}: {form_url[:90]}')
        result = 'pre_submission_error'
        try:
            page.goto(form_url, wait_until='domcontentloaded', timeout=30000)
            self.core.pause(3, 5)
            if identity and self.core.application_identity.application_key(page.url, self.core) != identity:
                raise self.core.UntrustedApplicationSite('ats_job_identity_changed')
            result = self.core.ats_adapter(ats, core).apply(page, job_id, title, company, form_url, history)
            return result
        except (self.core.UntrustedApplicationSite, self.core.UnsafeFormControl) as e:
            self.core.log(f'  ! unsafe application site/action: {e}')
            self.core.record_manual(job_id, title, company, url, str(e))
            result = 'external_manual'
            return 'external_manual'
        finally:
            protected = history.get(job_id, {}).get('status')
            ledger.finish(identity, job_id, protected if protected in self.core.UNCERTAIN_STATUSES else result)

    def apply_trusted_form(self, page: Page, job_id: str, title: str, company: str, form_url: str, ats: str, applied: dict | None=None) -> str:
        document = self.core.trusted_ats_document(page, ats)
        if ats == 'ashby':
            tab = document.get_by_role('tab', name=re.compile('application', re.I))
            if tab.count():
                self.core.click_form_control(tab.first)
                self.core.pause(1.5, 2.5)
        form = document.locator("form:has(input[type='file']), form:has(textarea), form:has(input[type='email'])").first
        if not form.count():
            form = document.locator('body')
        for attempt in range(2):
            try:
                self.core.check_ats_form(page, document, form, ats)
                unanswered = self.core.fill_step(page, form, external=True)
                self.core.pause(1, 2)
                self.core.check_ats_form(page, document, form, ats)
                unanswered += self.core.fill_step(page, form, external=True)
                break
            except self.core.ApplicationFormChanged:
                if document.count() != 1 or self.core.ats_provider(page.url) != ats:
                    raise self.core.UntrustedApplicationSite('document_changed_during_fill') from None
                if attempt:
                    raise
                self.core.log('  .. application form changed; reinspecting the same verified document once')
                self.core.pause(0.5, 1)
        self.core.check_ats_form(page, document, form, ats)
        if self.core.require_manual_review(page, form, unanswered, job_id, title, company, form_url, external=True):
            return 'manual_review'
        submit = form.locator("button[type='submit']:visible, input[type='submit']:visible")
        if not submit.count():
            submit = form.get_by_role('button', name=self.core.SUBMIT_RE)
        if not submit.count():
            self.core.screenshot(page, f'{job_id}_ext_nosubmit')
            self.core.record_manual(job_id, title, company, form_url, 'no_submit_button')
            return 'external_manual'
        if self.core.DRY_RUN:
            self.core.screenshot(page, f'{job_id}_ext_dryrun')
            return self.core.preview_application(job_id)
        self.core.check_ats_form(page, document, form, ats)
        before = self.core.application_submission_signals(page)

        def wait_result():
            result = self.core.wait_application_submission(page, before, expected_host=urlparse(form_url).hostname)
            return 'applied_external' if result == 'applied' else result
        return self.core.submit_application(page, submit.last, job_id, title, company, form_url, wait_result, applied)
