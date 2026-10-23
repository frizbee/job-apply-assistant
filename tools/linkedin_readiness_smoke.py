#!/usr/bin/env python3
"""Bounded live readiness check: saved regions, first saved title, up to 4 forms.

Forced TEST, isolated history/logs, no final Submit. Filling/uploading sends data.
"""
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ['DRY_RUN'] = 'true'
import apply
import linkedin_search
from playwright.sync_api import sync_playwright


def main():
    files = [apply.HOME / 'data' / name for name in
             ['applied.json', 'applications.csv', 'manual_apply.csv', 'user_settings.json', 'run.log']]
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in files if p.exists()}
    apply.load_settings()
    resume = Path(apply.SETTINGS['resume']).resolve()
    assert hashlib.sha256(resume.read_bytes()).hexdigest() == apply.SETTINGS['cv_hash'], 'CV changed'
    apply.apply_cv_profile(apply.SETTINGS['cv_profile'])
    apply.PROFILE['resume_file'] = str(resume)
    history = apply.load_applied()
    folder = apply.HOME / 'data/browser-diagnostics' / ('readiness-' + dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    folder.mkdir(parents=True)
    apply.APPLIED_FILE = folder / 'history.json'
    apply.APPLIED_FILE.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding='utf-8')
    apply.LOG_FILE, apply.MANUAL_FILE, apply.SHOTS = folder / 'applications.csv', folder / 'manual.csv', folder / 'shots'
    apply.log = lambda message: print(message, flush=True)
    apply.DRY_RUN = True
    apply.DEADLINE = time.time() + 720
    apply.CFG['limits'].update(max_applications_per_run=4, per_source=4)
    apply.reset_submission_attempts()
    apply.METRICS.reset()
    keyword, regions = apply.CFG['search']['keywords'][0], apply.selected_locations()
    report = {'forced_test_mode': True, 'keyword': keyword, 'regions': regions,
              'scope': 'first saved title, up to two pages per region, at most four Easy Apply forms',
              'searches': [], 'details': [], 'forms': [], 'final_submit_calls': 0, 'blocked_submission_requests': []}

    def forbidden(*args, **kwargs):
        report['final_submit_calls'] += 1
        raise RuntimeError('Final Submit disabled in readiness diagnostic')
    apply.submit_application = forbidden
    # Only Easy Apply forms in this diagnostic; external delivery is not tested.
    def no_external(*args, **kwargs):
        return 'external_not_tested'
    apply.apply_external = no_external
    with sync_playwright() as pw:
        ctx = apply.launch(pw)
        ctx.add_init_script("document.addEventListener('submit',e=>{e.preventDefault();e.stopImmediatePropagation()},true)")
        def guard(route):
            req = route.request
            if req.method in ('POST', 'PUT', 'PATCH', 'DELETE') and re.search(r'/(?:apply|applications?)(?:[/\?]|$)|submitApplication|createApplication', req.url, re.I):
                report['blocked_submission_requests'].append(req.url.split('?')[0])
                route.abort()
            else:
                route.continue_()
        ctx.route('**/*', guard)
        page = ctx.new_page()
        page.set_default_timeout(6000)
        page.set_default_navigation_timeout(30000)
        search = linkedin_search.LinkedInSearch(page, apply)
        candidates = {}
        try:
            for region in regions:
                candidates[region] = []
                for index in range(min(2, apply.CFG['search']['pages_per_search'])):
                    try:
                        result = search.search_page(keyword, region, index)
                        params = parse_qs(urlparse(search.page.url).query)
                        filters = search.page.locator('[componentkey="JobsSearchFilters"]')
                        remote = filters.locator('input[aria-label="Filter by Remote"]')
                        experience = filters.locator('[aria-label="Filter by Experience level"] input')
                        row = {'region': region, 'page': index + 1, 'verified_region': search.location_label(),
                               'geoId': params.get('geoId'), 'ids': result['ids'], 'count': len(result['ids']),
                               'remote': remote.first.is_checked() if remote.count() else None,
                               'experience_active': experience.first.is_checked() if experience.count() else None,
                               'experience_url': params.get('f_E'), 'posted_window': params.get('f_TPR')}
                        candidates[region].extend(result['ids'])
                        report['searches'].append(row)
                        print('SEARCH', json.dumps({k: v for k, v in row.items() if k != 'ids'}), flush=True)
                        if not result['has_next']:
                            break
                    except Exception as e:
                        report['searches'].append({'region': region, 'page': index + 1, 'error': type(e).__name__ + ': ' + str(e)[:350]})
                        break
            seen = set()
            for region in regions:
                if len(report['forms']) >= 4:
                    break
                # Bounded detail sample, one form per region where available.
                for job_id in candidates[region][:5]:
                    if job_id in seen:
                        continue
                    seen.add(job_id)
                    previous = history.get(job_id, {})
                    if previous and not apply.can_retry(previous.get('status', '')):
                        continue
                    try:
                        url = f'https://www.linkedin.com/jobs/view/{job_id}/'
                        page.goto(url, wait_until='domcontentloaded')
                        apply.pause(2, 3)
                        title, company, desc = apply.job_info(page)
                        reason = apply.job_ok(title, company, desc)
                        job = {'id': job_id, 'title': title, 'company': company, 'desc': desc, 'url': url,
                               'metadata': apply.read_job_metadata(page, 'linkedin', url)}
                        if not reason and apply.applied_elsewhere(history, title, company):
                            reason = 'applied_elsewhere'
                        eligible = not reason and apply.check_job_eligibility(job, history)
                        easy = page.get_by_role('button', name=apply.EASY_APPLY_RE).or_(page.locator(f'[componentkey="jdp-continue-button-{job_id}"]'))
                        row = {'region': region, 'job_id': job_id, 'title': title, 'company': company,
                               'filter_reason': reason, 'eligible': bool(eligible), 'easy_apply': bool(easy.count()),
                               'description_length': len(desc)}
                        report['details'].append(row)
                        print('DETAIL', json.dumps(row), flush=True)
                        if not eligible or not easy.count():
                            continue
                        with apply.submission_source('linkedin'), apply.METRICS.application(job_id):
                            status = apply.apply_to_job(page, job_id, title, company, history)
                        if status != 'dry_run' and history.get(job_id, {}).get('status') != status:
                            apply.record(history, job_id, title, company, status, url)
                        report['forms'].append({**row, 'status': status})
                        (folder / (job_id + '.html')).write_text(page.content(), encoding='utf-8')
                        print('FORM', json.dumps(report['forms'][-1]), flush=True)
                        break
                    except Exception as e:
                        report['details'].append({'region': region, 'job_id': job_id, 'error': type(e).__name__ + ': ' + str(e)[:400]})
        except Exception as e:
            report['fatal'] = type(e).__name__ + ': ' + str(e)[:500]
        finally:
            search.close()
            ctx.close()
    report['metrics'] = apply.METRICS.snapshot()
    report['production_files_unchanged'] = all(hashlib.sha256(p.read_bytes()).hexdigest() == h for p, h in before.items())
    (folder / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print('REPORT', folder, flush=True)
    print('SUMMARY', json.dumps({k: v for k, v in report.items() if k not in ('searches', 'details')}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
