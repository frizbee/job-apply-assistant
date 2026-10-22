#!/usr/bin/env python3
"""Opt-in bounded live smoke test. Never intentionally submit applications.

Run from the repository root: .venv/bin/python tools/linkedin_dry_run.py --job-id ID
Uses the app's saved browser login and profile. Diagnostics stay under ignored data/.
"""
import argparse
import csv
import datetime as dt
import json
import os
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ['DRY_RUN'] = 'true'
import apply


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--job-id', action='append', required=True)
    args = parser.parse_args()
    ids = list(dict.fromkeys(args.job_id))
    if len(ids) > 10 or any(not re.fullmatch(r'\d+', job_id) for job_id in ids):
        parser.error('Provide 1-10 numeric LinkedIn job IDs')
    apply.DRY_RUN = True
    apply.load_settings()
    if apply.SETTINGS.get('cv_profile'):
        apply.apply_cv_profile(apply.SETTINGS['cv_profile'])
    apply.PROFILE['resume_file'] = apply.SETTINGS['resume']
    apply.CFG['limits'].update(max_applications_per_run=1, per_source=1)
    applied = apply.load_applied()
    apply.reset_submission_attempts()
    results = []
    active = {}
    original_fill = apply.fill_step

    def observe_fill(page, form, external=False):
        active['form_opened'] = True
        result = original_fill(page, form, external=external)
        active['nonempty_fields'] = form.locator(apply.TEXT_FIELDS).evaluate_all("es => es.filter(e => !e.disabled && e.value.trim()).length")
        active['input_events'] = page.evaluate('window.__dryRunInputEvents || 0')
        active['unanswered'] = result
        return result

    apply.fill_step = observe_fill
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        ctx = apply.launch(pw)
        apply.begin_job_rechecks(applied)
        ctx.add_init_script("document.addEventListener('input', () => window.__dryRunInputEvents=(window.__dryRunInputEvents||0)+1, true); document.addEventListener('submit', e=>{e.preventDefault();e.stopImmediatePropagation()},true)")
        blocked = []
        def route(request_route):
            request = request_route.request
            if request.method in ('POST', 'PUT', 'PATCH', 'DELETE') and re.search(r'/(?:apply|applications?)(?:[/\?]|$)|submitApplication|createApplication', request.url, re.I):
                blocked.append(request.url.split('?')[0])
                request_route.abort()
            else:
                request_route.continue_()
        ctx.route('**/*', route)
        page = ctx.new_page()
        page.set_default_timeout(6000)
        try:
            for job_id in ids:
                if apply.submission_quota_status('linkedin'):
                    break
                active = {'id': job_id, 'form_opened': False, 'input_events': 0}
                if not apply.should_visit_job(applied, job_id):
                    active['result'] = 'history_or_recheck_limit'
                else:
                    assert apply.DRY_RUN is True
                    success = apply.process_job(page, applied, job_id)
                    active.update(result='dry_run' if success else applied.get(job_id, {}).get('status', 'not_completed'),
                                  final_url=page.url.split('?')[0])
                    if any(segment in page.url for segment in ('/login', '/checkpoint', '/authwall')):
                        active['blocker'] = 'User login or verification required'
                        results.append(active)
                        break
                results.append(active)
        finally:
            ctx.close()
    if apply.MANUAL_FILE.exists():
        with apply.MANUAL_FILE.open(encoding='utf-8', newline='') as stream:
            reasons = {row['job_id']: row for row in csv.DictReader(stream)}
        for result in results:
            result['reason'] = applied.get(result['id'], {}).get('reason')
            if result['id'] in reasons and applied.get(result['id'], {}).get('status') in ('metadata_manual', 'relevance_manual', 'external_manual', 'manual_review'):
                result['manual_reason'] = reasons[result['id']]['reason']
    report = {'at': dt.datetime.now(dt.timezone.utc).isoformat(), 'forced_dry_run': True,
              'results': results, 'blocked_submission_requests': blocked,
              'preview_count': apply.ATTEMPTS['total']}
    target = apply.HOME / 'data' / 'browser-diagnostics' / 'dry-run-report.json'
    target.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    archive = target.with_name('dry-run-' + dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '.json')
    archive.write_text(text, encoding='utf-8')
    target.write_text(text, encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
