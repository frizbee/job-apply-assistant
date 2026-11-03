#!/usr/bin/env python3
"""Bounded, submission-disabled Easy Apply probe with isolated history.

Private DOM evidence stays under ignored data/browser-diagnostics/.
"""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ['DRY_RUN'] = 'true'
import apply
from playwright.sync_api import sync_playwright


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--job-id', required=True)
    args = parser.parse_args()
    if not re.fullmatch(r'\d+', args.job_id):
        parser.error('Expected a numeric LinkedIn job ID')
    production = [apply.APPLIED_FILE, apply.LOG_FILE, apply.MANUAL_FILE, apply.LAST_RUN_FILE,
                  apply.HOME / 'data/user_settings.json', apply.HOME / 'data/run.log']
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in production if p.exists()}
    history = json.loads(apply.APPLIED_FILE.read_text()) if apply.APPLIED_FILE.exists() else {}
    if history.get(args.job_id, {}).get('status') in apply.UNCERTAIN_STATUSES or history.get(args.job_id, {}).get('status', '').startswith('applied'):
        parser.error('Protected application history: inspect application status manually first')
    folder = apply.HOME / 'data/browser-diagnostics' / ('form-probe-' + dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    folder.mkdir(parents=True)
    apply.load_settings()
    if apply.SETTINGS.get('cv_profile'):
        apply.apply_cv_profile(apply.SETTINGS['cv_profile'])
    apply.PROFILE['resume_file'] = apply.SETTINGS['resume']
    apply.DRY_RUN = True
    apply.DEADLINE = time.time() + 300
    apply.APPLIED_FILE = folder / 'applied.json'
    apply.LOG_FILE = folder / 'applications.csv'
    apply.MANUAL_FILE = folder / 'manual_apply.csv'
    apply.log = lambda message: print(message, flush=True)
    apply.reset_submission_attempts()
    apply.METRICS.reset()
    steps, blocked = [], []
    original_fill = apply.fill_step
    original_discard = apply.discard
    final_review = {}

    def forbidden(*args, **kwargs):
        raise RuntimeError('Final sending is disabled in this probe')

    apply.submit_application = apply.submit_linkedin = forbidden

    def capture(page, form, external=False):
        number = len(steps) + 1
        (folder / f'{number}-before.html').write_text(page.content(), encoding='utf-8')
        issues = original_fill(page, form, external=external)
        (folder / f'{number}-after.html').write_text(page.content(), encoding='utf-8')
        page.screenshot(path=str(folder / f'{number}-after.png'))
        row = {'step': number, 'issues': issues, 'text': form.inner_text()[:12000]}
        steps.append(row)
        print('STEP', number, 'ISSUES', json.dumps(issues), flush=True)
        return issues

    apply.fill_step = capture
    with sync_playwright() as pw:
        ctx = apply.launch(pw)
        ctx.set_default_timeout(6000)
        ctx.add_init_script("document.addEventListener('submit',e=>{e.preventDefault();e.stopImmediatePropagation()},true)")

        def guard(route):
            request = route.request
            if request.method in ('POST', 'PUT', 'PATCH', 'DELETE') and re.search(
                    r'/(?:apply|applications?)(?:[/\?]|$)|submitApplication|createApplication|OnsiteApplyApplications', request.url, re.I):
                blocked.append(request.url.split('?')[0])
                route.abort()
            else:
                route.continue_()

        ctx.route('**/*', guard)
        try:
            title = company = ''
            page = ctx.new_page()
            def discard(active_page):
                modal = active_page.locator(apply.MODAL).first
                primary = modal.locator('footer button:visible').last
                if modal.count() and primary.count() and apply.application_button_action(primary) == 'submit':
                    final_review['text'] = modal.inner_text()[:20000]
                    final_review['fields'] = modal.locator('input:not([type=hidden]),select,textarea').evaluate_all('''es=>es.map(e=>({
                        label:e.getAttribute('aria-label')||[...(e.labels||[])].map(l=>l.innerText).join(' '),
                        type:e.type,value:e.type==='radio'||e.type==='checkbox'?e.checked:e.value,
                        selected:e.tagName==='SELECT'?[...e.selectedOptions].map(o=>o.textContent):undefined
                    }))''')
                    active_page.screenshot(path=str(folder / 'final-review.png'))
                    (folder / 'final-review.html').write_text(active_page.content(), encoding='utf-8')
                return original_discard(active_page)
            apply.discard = discard
            page.goto(f'https://www.linkedin.com/jobs/view/{args.job_id}/', wait_until='domcontentloaded')
            page.wait_for_timeout(2500)
            title, company, _ = apply.job_info(page)
            result = apply.apply_to_job(page, args.job_id, title, company, {})
        except Exception as error:
            result = type(error).__name__ + ': ' + str(error)[:500]
            (folder / 'failure.html').write_text(page.content(), encoding='utf-8')
            page.screenshot(path=str(folder / 'failure.png'))
        finally:
            ctx.close()
    report = {'job_id': args.job_id, 'title': title, 'company': company, 'result': result,
              'forced_test_mode': True, 'steps': steps, 'blocked_submission_requests': blocked,
              'final_review': final_review,
              'production_unchanged': all(hashlib.sha256(p.read_bytes()).hexdigest() == digest for p, digest in before.items())}
    (folder / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print('RESULT', result, 'PRODUCTION_UNCHANGED', report['production_unchanged'])
    print('EVIDENCE', folder)


if __name__ == '__main__':
    main()
