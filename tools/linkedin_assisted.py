#!/usr/bin/env python3
"""Single-user, chat-assisted LinkedIn session. No sending without /ready consent.

Bind only to loopback; use the private token printed in the ignored session file.
All Playwright operations stay on the main thread. Browser fields are user-owned.
"""
import hashlib
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import queue
import secrets
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import apply
import deferred_review
import job_metadata
import linkedin_search
import resume_match
import application_policy
import candidate_workflow
import policy_discovery
from playwright.sync_api import sync_playwright

TITLES = application_policy.TITLES


class Assisted:
    def __init__(self, context):
        self.context = context
        self.search_page = context.new_page()
        self.page = context.new_page()
        self.page.add_init_script(deferred_review.GUARD_JS)
        self.batches = policy_discovery.job_lists(apply, self.search_page)
        self.pending, self.seen, self.current = [], set(), None
        self.reviewing_job_id = None
        self.processing_job_id = None
        self.history = apply.load_applied()
        self.cv = apply.ROOT / 'CV_Andre2026.pdf'
        self.cv_hash = hashlib.sha256(self.cv.read_bytes()).hexdigest()

    def status(self):
        return {'current': self.current, 'url': self.page.url, 'seen': len(self.seen),
                'titles': TITLES, 'resume': self.cv.name,
                'queue_file': str(candidate_workflow.store(apply).path), 'test_mode': apply.DRY_RUN}

    def search_next(self):
        if self.current:
            return {'error': 'Finish or explicitly skip the current application first', **self.status()}
        checked = []
        deadline = time.monotonic() + 70
        while time.monotonic() < deadline:
            if not self.pending:
                try:
                    self.pending.extend(next(self.batches))
                except StopIteration:
                    return {'exhausted': True, 'checked': checked}
                if not self.pending:
                    continue
            job_id = self.pending.pop(0)
            if job_id in self.seen:
                continue
            self.seen.add(job_id)
            previous = self.history.get(job_id)
            explicitly_reviewing = (job_id == self.reviewing_job_id and previous and previous['status'] in deferred_review.DEFERRED)
            if previous and not apply.can_retry(previous['status']) and not application_policy.reassess(apply, previous) and not explicitly_reviewing:
                continue
            url = f'https://www.linkedin.com/jobs/view/{job_id}/'
            self.processing_job_id = job_id
            self.page.goto(url, wait_until='domcontentloaded')
            self.page.wait_for_timeout(1800)
            title, company, desc = apply.job_info(self.page)
            diagnostic = apply.HOME / 'data/browser-diagnostics' / ('assisted-job-' + job_id + '.html')
            diagnostic.parent.mkdir(parents=True, exist_ok=True)
            diagnostic.touch(mode=0o600, exist_ok=True)
            diagnostic.chmod(0o600)
            diagnostic.write_text(self.page.content(), encoding='utf-8')
            metadata = apply.read_job_metadata(self.page, 'linkedin', url)
            job = dict(id=job_id, title=title, company=company, url=url, desc=desc, metadata=metadata)
            entry = candidate_workflow.evaluate(apply, job)
            evidence = entry['assessment']
            reason = (apply.job_ok(title, company, desc) or
                      ('applied_elsewhere' if apply.applied_elsewhere(self.history, title, company) else None) or
                      ('job_closed' if metadata.get('active') is False else None) or
                      job_metadata.eligibility_reason(metadata, None, True) or
                      (entry['status'] if entry['status'] != 'eligible' else None))
            row = {'job_id': job_id, 'title': title, 'company': company, 'url': url, 'description_length': len(desc),
                   'reason': reason, 'match': evidence}
            checked.append(row)
            if reason:
                candidate_workflow.save(apply, job, reason=reason,
                    status='needs_assessment' if reason == 'needs_assessment' else
                           'eligible_blocked' if entry.get('qualifies') else entry['status'])
                self.processing_job_id = None
                continue
            result = self.open_form(row)
            self.processing_job_id = None
            return {**result, 'checked': checked}
        return {'continue_search': True, 'checked': checked, **self.status()}

    def open_form(self, row):
        """External routing is independent of suitability; never discard no-Easy-Apply."""
        self.history = apply.load_applied()
        apply.mark_evaluation_passed(self.history, row['job_id'], row['title'], row['company'], row['url'])
        opener = self.page.get_by_role('button', name=apply.EASY_APPLY_RE).first
        kind = 'linkedin'
        self.ats_document = None
        if opener.count():
            apply.click_form_control(opener)
            form = self.page.locator(apply.MODAL).first
            form.wait_for(timeout=10000)
        else:
            destination = apply.external_url(self.page)
            if not destination or not apply.safe_https_url(destination):
                candidate_workflow.store(apply).put(row['job_id'], status='eligible_blocked',
                    reason='External application URL unavailable or unsafe; manual link review required')
                return {'manual_link_review': True, **self.status()}
            self.page.goto(destination, wait_until='domcontentloaded')
            self.page.wait_for_timeout(1500)
            if not apply.safe_https_url(self.page.url):
                return {'error': 'Unsafe external redirect; candidate preserved'}
            ats = apply.ats_provider(self.page.url)
            heading_texts = self.page.locator('h1:visible, h2:visible, [data-testid="job-title"]:visible').all_inner_texts()
            identity_verified = (any(resume_match.normalized(text) == resume_match.normalized(row['title']) for text in heading_texts)
                                 and application_policy.contains(row['company'], self.page.title() + '\n' + self.page.locator('body').inner_text()))
            kind = 'external_trusted' if ats and identity_verified else 'external_manual'
            if kind == 'external_trusted':
                # Bind manual data-entry checks/submission to this approved ATS document.
                self.ats_document = apply.trusted_ats_document(self.page, ats)
                form = self.ats_document.locator("form:has(input[type='file']), form:has(textarea), form:has(input[type='email'])").first
                if not form.count():
                    form = self.ats_document.locator('body')
                apply.check_ats_form(self.page, self.ats_document, form, ats)
            else:
                form = self.page.locator('body')
        self.form = form
        self.current = {**row, 'kind': kind, 'document_url': self.page.url, 'step': secrets.token_hex(12),
                        'form_text': form.inner_text()[:14000]}
        self.document = self.page.evaluate_handle('document')
        self.page.bring_to_front()
        candidate_workflow.store(apply).put(row['job_id'], status='waiting_for_user',
            application_url=self.page.url, application_kind=kind, activity='apply_control_opened')
        return {'waiting_for_user': True, **self.status()}

    def open_saved(self, body):
        if self.current:
            return {'error': 'Current application must be finished or skipped first'}
        job_id = body.get('job_id')
        entry = candidate_workflow.store(apply).load()['jobs'].get(job_id)
        if (not entry or not entry.get('qualifies') or entry.get('blockers')
                or entry.get('status') in ('submission_pending', 'submission_unconfirmed', 'submission_rejected', 'reported_sent')
                or entry.get('status', '').startswith('applied')):
            return {'error': 'Candidate is not eligible/unblocked'}
        self.history = apply.load_applied()
        previous = self.history.get(job_id)
        if (previous and not apply.can_retry(previous['status']) and previous['status'] not in deferred_review.DEFERRED
                and not application_policy.reassess(apply, previous)):
            return {'error': 'Protected application history; check status manually'}
        # Rediscover the current JD and revalidate, rather than opening a stale saved URL.
        self.pending.insert(0, job_id)
        self.seen.discard(job_id)
        self.reviewing_job_id = job_id
        return self.search_next()

    def ready(self, body):
        current = self.current
        if (not current or body.get('job_id') != current['job_id'] or
                body.get('step') != current['step'] or body.get('consent') is not True):
            return {'error': 'Explicit approval of the current application step is required'}
        if (hashlib.sha256(self.cv.read_bytes()).hexdigest() != self.cv_hash or
                self.page.url != current['document_url'] or
                not self.document.evaluate('(d)=>d===document')):
            return {'error': 'CV or application document changed; do not submit'}
        if current.get('kind') == 'external_manual':
            return {'manual_submission_required': True,
                    'instruction': 'Unknown site: automatic Submit is disabled. Ask the user to authorize manual sending; use /manual-send only after that consent.',
                    **self.status()}
        modal = self.form
        if not modal.count() or not modal.is_visible():
            return {'error': 'Application form is no longer open'}
        issues = apply.native_form_issues(modal)
        if apply.has_errors(modal):
            issues.append('LinkedIn reports validation errors')
        if self.page.locator(apply.CAPTCHA_SEL).count():
            issues.append('Complete site verification')
        if issues:
            return {'issues': issues, **self.status()}
        external = current.get('kind') == 'external_trusted'
        if external:
            apply.check_ats_form(self.page, self.ats_document, modal, apply.ats_provider(self.page.url))
            primary = modal.locator("button[type='submit']:visible, input[type='submit']:visible")
            if primary.count() != 1:
                return {'error': 'Cannot identify one final external Submit control; manual review required'}
        else:
            primary = modal.locator('footer button:visible').last
        if not primary.count() or not primary.is_enabled():
            return {'error': 'No enabled action button'}
        action = apply.application_button_action(primary)
        if action == 'next':
            apply.click_navigation(primary)
            self.page.wait_for_timeout(1600)
            current.update(step=secrets.token_hex(12), form_text=modal.inner_text()[:14000])
            return {'waiting_for_user': True, **self.status()}
        if action != 'submit':
            return {'error': 'Unknown application action; no click performed'}
        # The normal guard persists pending before clicking and verifies confirmation.
        self.history = apply.load_applied()
        session = self
        class ConfirmedStep:
            # Authorize only an explicitly reopened deferred item and this exact
            # user-approved step; normal sending still uses the durable guard.
            job_id = current['job_id']

            def wait(self, core, page, scope, issues):
                return (scope.count() == 1 and scope.is_visible() and page.url == current['document_url']
                        and session.document.evaluate('(d)=>d===document')
                        and hashlib.sha256(session.cv.read_bytes()).hexdigest() == session.cv_hash
                        and not core.native_form_issues(scope) and not core.has_errors(scope))

        previous = self.history.get(current['job_id'], {})
        context_token = None
        if current['job_id'] == self.reviewing_job_id and previous.get('status') in deferred_review.DEFERRED:
            context_token = deferred_review.CURRENT.set(ConfirmedStep())
        try:
            self.page.evaluate('''() => { window.__autoapplyManualClickUsed = false;
                window.__autoapplyManualSubmitUsed = false; window.__autoapplyManualAllowSubmit = true; }''')
            if external:
                before = apply.application_submission_signals(self.page)
                host = urlparse(self.page.url).hostname
                def wait_result():
                    result = apply.wait_application_submission(self.page, before, expected_host=host)
                    return 'applied_external' if result == 'applied' else result
                status = apply.submit_application(self.page, primary, current['job_id'], current['title'],
                    current['company'], self.page.url, wait_result, self.history)
            else:
                status = apply.submit_linkedin(self.page, modal, primary, current['job_id'],
                                               current['title'], current['company'], self.history)
        finally:
            if context_token is not None:
                deferred_review.CURRENT.reset(context_token)
            try:
                self.page.evaluate('window.__autoapplyManualAllowSubmit = false')
            except Exception:
                pass
        result = {'result': status, 'job': current}
        if status in ('applied', 'applied_external', 'submission_pending', 'submission_unconfirmed', 'submission_rejected'):
            self.current = None
            self.reviewing_job_id = None
        return result

    def skip(self, body):
        if not self.current or body.get('job_id') != self.current['job_id'] or body.get('step') != self.current['step']:
            return {'error': 'Wrong application/step'}
        if self.current.get('manual_send_authorized'):
            return {'error': 'Manual send is pending; check/report its outcome before moving on'}
        candidate_workflow.store(apply).put(self.current['job_id'], status='eligible', reason='User postponed this form')
        self.current = None
        return {'postponed': True}

    def manual_send(self, body):
        current = self.current
        if (not current or current.get('kind') != 'external_manual' or body.get('consent') is not True
                or body.get('job_id') != current['job_id'] or body.get('step') != current['step']):
            return {'error': 'Explicit manual-send consent required for this application'}
        if apply.DRY_RUN:
            return {'error': 'Test mode forbids manual sending'}
        if time.time() > apply.DEADLINE:
            return {'error': 'Runtime limit reached before manual sending'}
        if not candidate_workflow.submission_allowed(apply, current['job_id']):
            return {'error': 'Candidate assessment is stale, blocked or incomplete'}
        if self.page.url != current['document_url'] or not self.document.evaluate('(d)=>d===document'):
            return {'error': 'Unknown site document changed; re-review it first'}
        history = apply.load_applied()
        previous = history.get(current['job_id'])
        if (previous and not apply.can_retry(previous['status']) and not
                (current['job_id'] == self.reviewing_job_id and previous['status'] in deferred_review.DEFERRED)):
            return {'error': 'Protected history; check application status manually'}
        denied = apply.reserve_submission_attempt('linkedin')
        if denied:
            return {'error': denied}
        apply.record(history, current['job_id'], current['title'], current['company'],
                     'submission_pending', self.page.url)
        self.page.evaluate('''() => { window.__autoapplyManualClickUsed = false;
            window.__autoapplyManualSubmitUsed = false; window.__autoapplyManualAllowSubmit = true; }''')
        current['manual_send_authorized'] = True
        return {'user_must_click_submit': True, 'tracking': 'submission_pending', **self.status()}

    def reported_sent(self, body):
        current = self.current
        if (not current or not current.get('manual_send_authorized') or body.get('reported_sent') is not True
                or body.get('job_id') != current['job_id']):
            return {'error': 'Explicit report of the current manual submission required'}
        history = apply.load_applied()
        apply.record(history, current['job_id'], current['title'], current['company'],
                     'submission_unconfirmed', current['document_url'])
        candidate_workflow.store(apply).put(current['job_id'], status='reported_sent',
                                           reason='User reported sending; not independently confirmed')
        self.current = None
        return {'reported_sent': True, 'independently_confirmed': False}


def main():
    apply.load_settings()
    if apply.SETTINGS.get('cv_profile'):
        apply.apply_cv_profile(apply.SETTINGS['cv_profile'])
    application_policy.activate(apply)
    apply.DRY_RUN = bool(apply.SETTINGS.get('test_mode', True))
    apply.DEADLINE = time.time() + 4 * 3600
    apply.reset_submission_attempts()
    token = secrets.token_urlsafe(32)
    tasks = queue.Queue()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            if self.headers.get('Authorization') != 'Bearer ' + token:
                self.send_error(403)
                return
            body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0'))) or '{}')
            result = queue.Queue()
            tasks.put((self.path, body, result))
            answer = result.get(timeout=900)
            data = json.dumps(answer, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    with sync_playwright() as pw:
        context = apply.launch(pw)
        context.set_default_timeout(6000)
        context.set_default_navigation_timeout(30000)
        session = Assisted(context)
        session.search_page.goto('https://www.linkedin.com/jobs/', wait_until='domcontentloaded')
        state = apply.HOME / 'data/browser-diagnostics/assisted-session.json'
        state.parent.mkdir(parents=True, exist_ok=True)
        state.touch(mode=0o600)
        state.chmod(0o600)
        state.write_text(json.dumps({'port': server.server_port, 'token': token}), encoding='utf-8')
        threading.Thread(target=server.serve_forever, daemon=True).start()
        print('Assisted browser ready; no submissions without an approved /ready request.', flush=True)
        while time.time() < apply.DEADLINE:
            try:
                path, body, result = tasks.get_nowait()
            except queue.Empty:
                session.search_page.wait_for_timeout(200)
                continue
            try:
                answer = (session.status() if path == '/status' else
                          session.search_next() if path == '/search' else
                          session.ready(body) if path == '/ready' else
                          session.open_saved(body) if path == '/open' else
                          session.skip(body) if path == '/skip' else
                          session.manual_send(body) if path == '/manual-send' else
                          session.reported_sent(body) if path == '/reported-sent' else
                          candidate_workflow.store(apply).load() if path == '/queue' else
                          candidate_workflow.apply_review(apply, body['job_id'], body['review']) if path == '/assessment' else
                          {'error': 'Unknown command'})
            except Exception as exc:
                if session.processing_job_id is not None and session.current is None:
                    candidate_workflow.store(apply).put(session.processing_job_id, status='discovery_error',
                        job_url=f'https://www.linkedin.com/jobs/view/{session.processing_job_id}/',
                        reason='Job processing failed: ' + type(exc).__name__)
                    session.processing_job_id = None
                answer = {'error': type(exc).__name__ + ': ' + str(exc)[:600], **session.status()}
            result.put(answer)
        server.shutdown()
        context.close()


if __name__ == '__main__':
    main()
