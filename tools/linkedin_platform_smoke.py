#!/usr/bin/env python3
"""Bounded, forced-test LinkedIn form diagnostics. Never calls final Submit.

Filling/uploading may transmit data to LinkedIn. Private evidence stays in data/.
Usage: .venv/bin/python tools/linkedin_platform_smoke.py --job-id ID [--job-id ID]
"""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ['DRY_RUN'] = 'true'
import apply
from playwright.sync_api import sync_playwright


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--job-id', action='append', required=True)
    args = parser.parse_args()
    if len(args.job_id) > 4 or any(not re.fullmatch(r'[0-9]+', key) for key in args.job_id):
        parser.error('Provide at most four numeric IDs')
    root = apply.HOME
    files = [root / 'data' / name for name in ['applied.json', 'applications.csv', 'manual_apply.csv', 'user_settings.json', 'run.log']]
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in files if p.exists()}
    apply.load_settings()
    resume = Path(apply.SETTINGS['resume']).resolve()
    assert hashlib.sha256(resume.read_bytes()).hexdigest() == apply.SETTINGS['cv_hash'], 'CV changed'
    apply.apply_cv_profile(apply.SETTINGS['cv_profile'])
    apply.PROFILE['resume_file'] = str(resume)
    history = apply.load_applied()
    for key in args.job_id:
        previous = history.get(key, {})
        assert not (previous.get('status', '').startswith('applied') or previous.get('status') in apply.UNCERTAIN_STATUSES), 'Protected attempt'
    folder = root / 'data' / 'browser-diagnostics' / ('platform-smoke-' + dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    folder.mkdir(parents=True)
    apply.APPLIED_FILE = folder / 'history-copy.json'
    apply.APPLIED_FILE.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding='utf-8')
    apply.LOG_FILE = folder / 'applications.csv'
    apply.MANUAL_FILE = folder / 'manual.csv'
    apply.SHOTS = folder / 'shots'
    apply.log = lambda message: print(message, flush=True)
    apply.DRY_RUN, apply.EXTERNAL = True, False
    apply.CFG['limits'].update(max_applications_per_run=len(args.job_id), per_source=len(args.job_id))
    apply.reset_submission_attempts()
    apply.METRICS.reset()
    final_calls = []
    def disabled_submit(*a, **kw):
        final_calls.append(True)
        raise RuntimeError('Final Submit disabled in diagnostic')
    apply.submit_application = disabled_submit
    results, blocked = [], []
    original = apply.fill_step
    active = {}
    def observe(page, form, external=False):
        issues = original(page, form, external=external)
        active['steps'].append({'issues': issues, 'resume_cards': form.locator('[componentkey="easyApplyUploadedResumeRef"]').count()})
        target = folder / (active['job_id'] + '-step-' + str(len(active['steps'])))
        target.with_suffix('.html').write_text(page.content(), encoding='utf-8')
        page.screenshot(path=str(target.with_suffix('.png')))
        return issues
    apply.fill_step = observe
    with sync_playwright() as pw:
        ctx = apply.launch(pw)
        ctx.add_init_script("document.addEventListener('submit',e=>{e.preventDefault();e.stopImmediatePropagation()},true)")
        def guard(route):
            req = route.request
            if req.method in ('POST', 'PUT', 'PATCH', 'DELETE') and re.search(r'/(?:apply|applications?)(?:[/\?]|$)|submitApplication|createApplication', req.url, re.I):
                blocked.append(req.url.split('?')[0])
                route.abort()
            else:
                route.continue_()
        ctx.route('**/*', guard)
        page = ctx.new_page()
        page.set_default_timeout(6000)
        try:
            for job_id in args.job_id:
                active = {'job_id': job_id, 'steps': []}
                try:
                    url = f'https://www.linkedin.com/jobs/view/{job_id}/'
                    page.goto(url, wait_until='domcontentloaded', timeout=30000)
                    page.get_by_role('button', name=apply.EASY_APPLY_RE).or_(page.locator(f'[componentkey="jdp-continue-button-{job_id}"]')).first.wait_for(state='visible', timeout=15000)
                    title, company, desc = apply.job_info(page)
                    reason = apply.job_ok(title, company, desc)
                    job = {'id': job_id, 'title': title, 'company': company, 'desc': desc, 'url': url, 'metadata': apply.read_job_metadata(page, 'linkedin', url)}
                    if reason or not apply.check_job_eligibility(job, history):
                        active['result'] = 'eligibility_blocked:' + str(reason or 'metadata')
                    else:
                        with apply.submission_source('linkedin'), apply.METRICS.application(job_id):
                            active['result'] = apply.apply_to_job(page, job_id, title, company, history)
                except Exception as e:
                    active['error'] = type(e).__name__ + ': ' + str(e)[:500]
                results.append(active)
                print(json.dumps(active, ensure_ascii=False), flush=True)
        finally:
            ctx.close()
    report = {'forced_test_mode': True, 'results': results, 'metrics': apply.METRICS.snapshot(),
              'final_submit_calls': len(final_calls), 'blocked_submission_requests': blocked,
              'production_files_unchanged': all(hashlib.sha256(p.read_bytes()).hexdigest() == value for p, value in before.items())}
    (folder / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print('DIAGNOSTICS', folder)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
