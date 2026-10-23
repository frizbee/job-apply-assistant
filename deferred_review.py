"""A history-backed queue and one-job, user-assisted form review sessions."""
from contextvars import ContextVar
import csv
import hashlib
from pathlib import Path
import secrets
import threading
import time


DEFERRED = frozenset({'manual_review', 'external_manual'})
CURRENT = ContextVar('deferred_review', default=None)


def queue(history, manual_file):
    """History is authoritative: never requeue pending/uncertain/sent attempts."""
    notes = {}
    if manual_file.exists():
        with manual_file.open(encoding='utf-8', newline='') as stream:
            notes = {row['job_id']: row for row in csv.DictReader(stream) if row.get('job_id')}
    return [dict(job_id=job_id, title=entry.get('title', ''), company=entry.get('company', ''),
                 status=entry['status'], job_url=entry.get('url', ''),
                 apply_url=notes.get(job_id, {}).get('apply_url') or entry.get('url', ''),
                 reason=notes.get(job_id, {}).get('reason') or entry.get('reason', entry['status']))
            for job_id, entry in history.items() if entry.get('status') in DEFERRED]


def authorized(job_id, previous):
    session = CURRENT.get()
    return bool(session and session.job_id == job_id and previous and previous.get('status') in DEFERRED)


class Session:
    def __init__(self, job_id, test_mode, cv_hash=None):
        self.job_id, self.test_mode = job_id, test_mode
        self.cv_hash = cv_hash
        self.lock = threading.Lock()
        self.state = {'job_id': job_id, 'test_mode': test_mode, 'phase': 'opening', 'issues': []}
        self.command = None

    def public(self):
        with self.lock:
            return {**self.state, 'issues': list(self.state['issues'])}

    def action(self, action, step):
        with self.lock:
            if self.state['phase'] != 'waiting' or self.state.get('step') != step or self.command:
                return {'error': 'review_not_waiting'}
            if action not in ('continue', 'skip'):
                return {'error': 'invalid_review_action'}
            self.command = action
        return {'ok': True}

    def wait(self, core, page, form, issues):
        # Unsafe navigation/ambiguous controls must not become safe merely by
        # confirming form values. Only ordinary unanswered fields are assisted.
        if any(any(word in issue.lower() for word in ('unsafe', 'submit-capable', 'unrecognized application action')) for issue in issues):
            return False
        original = form.element_handle(timeout=2000)
        document = page.evaluate_handle('document')
        url = page.url
        step = secrets.token_hex(12)
        with self.lock:
            self.state.update(phase='waiting', step=step, issues=list(issues))
            self.command = None
        core.log('  ? Deferred review: fill this step in Chrome, then use Check and continue in the GUI. Do not click Submit in Chrome.')
        try:
            while True:
                if core.STOP.is_set():
                    raise core.Stopped
                if time.time() > core.DEADLINE:
                    raise core.LimitReached
                # A new document/form is not covered by this step's confirmation.
                try:
                    same = document.evaluate('(d, url) => d === document && location.href === url', url)
                    connected = original.evaluate('e => e.isConnected')
                    current = form.evaluate('(e, original) => e === original', original) if same and connected else False
                except Exception:
                    return False
                if not same or not connected or not current:
                    return False
                with self.lock:
                    command, self.command = self.command, None
                if command == 'skip':
                    return False
                if command == 'continue':
                    if self.cv_hash and hashlib.sha256(Path(core.SETTINGS['resume']).read_bytes()).hexdigest() != self.cv_hash:
                        raise RuntimeError('CV changed during review; stop and review the new CV first')
                    remaining = core.native_form_issues(form)
                    if page.locator(core.CAPTCHA_SEL).count():
                        remaining.append('Complete the site verification in the browser')
                    if core.has_errors(form):
                        remaining.append('The site still reports validation errors')
                    if remaining:
                        with self.lock:
                            self.state.update(issues=list(dict.fromkeys(remaining)), step=secrets.token_hex(12))
                        core.log('  ? Complete the remaining required fields, then check this step again.')
                    else:
                        with self.lock:
                            self.state.update(phase='continuing', issues=[])
                        return True  # Explicit approval of this form/step only.
                page.wait_for_timeout(250)  # Pump Playwright; never block its event loop with Event.wait().
        finally:
            for handle in (original, document):
                try:
                    handle.dispose()
                except Exception:
                    pass

    def finish(self, result):
        with self.lock:
            self.state.update(phase='finished', result=result, issues=[])
            self.command = None


GUARD_JS = r"""(() => {
    window.__autoapplyManualAllowSubmit = false;
    window.__autoapplyManualClickUsed = false;
    window.__autoapplyManualSubmitUsed = false;
    document.addEventListener('submit', e => {
        if (window.__autoapplyManualAllowSubmit && !window.__autoapplyManualSubmitUsed) {
            window.__autoapplyManualSubmitUsed = true;
            return;
        }
        e.preventDefault(); e.stopImmediatePropagation();
    }, true);
    document.addEventListener('click', e => {
        const b = e.target.closest('button,input[type=submit],[role=button]');
        if (!b) return;
        const labels = [b.innerText || b.value || '', b.getAttribute('aria-label') || ''];
        if ((b.tagName === 'BUTTON' && b.type === 'submit') || b.matches('input[type=submit]') || labels.some(s => /^\s*(submit(?:\s+(?:your\s+)?application)?|send\s+(?:your\s+)?application|başvuruyu gönder)\s*$/i.test(s))) {
            if (window.__autoapplyManualAllowSubmit && !window.__autoapplyManualClickUsed) {
                window.__autoapplyManualClickUsed = true;
                return;
            }
            e.preventDefault(); e.stopImmediatePropagation();
        }
    }, true);
})()"""


def run(core, row, session):
    """Reopen exactly one selected job; normal bulk runs never enter this context."""
    from playwright.sync_api import sync_playwright
    history = core.load_applied()
    if history.get(session.job_id, {}).get('status') not in DEFERRED:
        session.finish('history_protected_or_no_longer_deferred')
        return
    token = CURRENT.set(session)
    old_limits = dict(core.CFG['limits'])
    old_replace = core.SETTINGS.get('replace_prefilled_contacts', False)
    had_replace = 'replace_prefilled_contacts' in core.SETTINGS
    old_external = core.EXTERNAL
    try:
        core.CFG['limits'].update(max_applications_per_run=1, per_source=1)
        core.SETTINGS['replace_prefilled_contacts'] = False
        core.EXTERNAL = True  # Explicitly selected, approved ATS only; never a bulk preference.
        core.DEADLINE = time.time() + core.CFG['limits']['max_runtime_minutes'] * 60
        core.reset_submission_attempts()
        core.METRICS.reset()
        core.EXT_STATE['count'] = 0
        if core.STOP.is_set():
            raise core.Stopped
        with sync_playwright() as pw:
            ctx = core.launch(pw)
            try:
                ctx.set_default_timeout(6000)
                ctx.set_default_navigation_timeout(30000)
                page = ctx.new_page()
                core.log(f"Reviewing one deferred job ({'TEST: no sending' if core.DRY_RUN else 'LIVE: sending is allowed after step confirmation'}): {row['title']} @ {row['company']}")
                source = core.source_for_job(session.job_id)
                with core.submission_source(source), core.METRICS.application(session.job_id):
                    if session.job_id.isdigit():
                        if not core.linkedin_login(page):
                            session.finish('login_required')
                            return
                        # Login must remain possible; install the form guard
                        # only after authentication, before opening the job.
                        ctx.add_init_script(GUARD_JS)
                        success = core.process_job(page, history, session.job_id)
                        result = 'dry_run' if success and core.DRY_RUN else history.get(session.job_id, {}).get('status', 'not_completed')
                    elif core.ats_provider(row['apply_url']):
                        ctx.add_init_script(GUARD_JS)
                        page.goto(row['job_url'], wait_until='domcontentloaded')
                        job = {'id': session.job_id, 'title': row['title'], 'company': row['company'], 'url': row['job_url'],
                               'metadata': core.read_job_metadata(page, source, row['job_url'])}
                        description = job['metadata'].get('description') or ''
                        reason = (core.job_ok(row['title'], row['company'], description)
                                  or ('applied_elsewhere' if core.applied_elsewhere(history, row['title'], row['company']) else None))
                        if reason:
                            core.record_relevance_decision(history, session.job_id, row['title'], row['company'], reason, row['job_url'], description)
                            result = history[session.job_id]['status']
                        elif not core.check_job_eligibility(job, history):
                            result = history[session.job_id]['status']
                        else:
                            result = core.apply_form(page, session.job_id, row['title'], row['company'], row['apply_url'], history)
                            if result != 'dry_run' and history.get(session.job_id, {}).get('status') != result:
                                core.record(history, session.job_id, row['title'], row['company'], result, row['job_url'])
                    else:
                        result = 'unsupported_manual_site'
                session.finish(result)
            finally:
                ctx.close()
    except core.Stopped:
        session.finish('stopped; remains deferred unless a submission was attempted')
    except Exception as e:
        core.log(f'Deferred review stopped: {e.__class__.__name__}')
        session.finish('review_stopped; check history before retrying')
    finally:
        core.CFG['limits'].clear()
        core.CFG['limits'].update(old_limits)
        if had_replace:
            core.SETTINGS['replace_prefilled_contacts'] = old_replace
        else:
            core.SETTINGS.pop('replace_prefilled_contacts', None)
        core.EXTERNAL = old_external
        CURRENT.reset(token)
