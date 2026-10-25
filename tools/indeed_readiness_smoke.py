#!/usr/bin/env python3
"""Bounded Indeed access/search diagnostic through the production adapter.

Read-only: no Apply actions, filling, uploads, final Submit or production writes.
Uses the first saved title and the adapter's existing Remote search. Does not
claim that this search implements the user's selected countries/regions.
Manual login/challenge completion is possible in the visible browser; no bypass.
"""
import argparse
import datetime as dt
import hashlib
import itertools
import json
import os
from pathlib import Path
import signal
import sys
import time
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ['DRY_RUN'] = 'true'
import apply
import sources
from playwright.sync_api import sync_playwright
from tools.indeed_browser_session import reuse_page, is_indeed_url, diagnostic_url


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wait-seconds', type=int, default=90)
    parser.add_argument('--manual-login', action='store_true',
                        help='Keep the same window open for manual login; inspect it without another goto afterwards')
    args = parser.parse_args()
    if not 5 <= args.wait_seconds <= 600:
        parser.error('Use a bounded wait of 5–600 seconds')
    files = [apply.HOME / 'data' / name for name in
             ['applied.json', 'applications.csv', 'manual_apply.csv', 'user_settings.json', 'run.log']]
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in files if p.exists()}
    apply.load_settings()
    apply.apply_cv_profile(apply.SETTINGS['cv_profile'])
    apply.DRY_RUN = True
    apply.DEADLINE = time.time() + args.wait_seconds + 60
    apply.CFG['search']['keywords'] = apply.CFG['search']['keywords'][:1]
    apply.log = lambda message: print(message, flush=True)
    # Background shells may inherit SIGINT ignored. Convert explicit stop
    # signals into the regular cleanup/report path rather than killing the driver.
    for stop_signal in (signal.SIGINT, signal.SIGTERM):
        signal.signal(stop_signal, lambda *_args: apply.STOP.set())
    sources.core = apply
    folder = apply.HOME / 'data/browser-diagnostics' / ('indeed-access-' + dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    folder.mkdir(parents=True)
    report = {'forced_test_mode': True, 'search_only': True,
              'keyword': apply.CFG['search']['keywords'][0], 'requested_saved_regions': apply.selected_locations(),
              'region_mapping_verified': False, 'wait_seconds': args.wait_seconds, 'request_interception': False,
              'responses': [], 'observations': [], 'cards': [], 'submit_calls': 0, 'application_actions': 0}

    def forbidden(*args, **kwargs):
        report['submit_calls'] += 1
        raise RuntimeError('Final Submit disabled in Indeed access diagnostic')
    apply.submit_application = forbidden

    def bounded_wait(page, predicate, message, minutes=10):
        print('MANUAL_WAIT', message, flush=True)
        deadline = time.monotonic() + args.wait_seconds
        while time.monotonic() < deadline:
            if apply.STOP.is_set() or (folder / 'stop').exists():
                raise apply.Stopped
            ready = predicate(page)
            report['observations'].append({'url': diagnostic_url(page.url), 'ready': bool(ready),
                                           'blocked': apply.blocked(page), 'auth_pending': sources.indeed_auth_pending(page)})
            if ready:
                return True
            page.wait_for_timeout(1000)
        return False
    apply.wait_human = bounded_wait
    with sync_playwright() as pw:
        ctx = apply.launch(pw)
        # Production search does not intercept requests. This diagnostic only
        # consumes jobs from the generator; it never invokes application callbacks.
        page, report['reused_tab'] = reuse_page(ctx, apply.CFG['sources'].get('indeed_domain', 'www.indeed.com'))
        page.set_default_timeout(6000)
        page.set_default_navigation_timeout(30000)
        def response(resp):
            if resp.request.is_navigation_request() and resp.request.frame == page.main_frame:
                parsed = urlparse(resp.url)
                entry = {'url': diagnostic_url(resp.url), 'status': resp.status}
                if parsed.path == '/bff/oauth/callback':
                    params = parse_qs(parsed.query)
                    entry.update(oauth_code_present=bool(params.get('code')), oauth_error_present=bool(params.get('error')))
                report['responses'].append(entry)
        page.on('response', response)
        navigations, reloads = [], []
        original_goto, original_reload = page.goto, page.reload
        def goto(url, *args, **kwargs):
            navigations.append(url)
            return original_goto(url, *args, **kwargs)
        def reload(*args, **kwargs):
            reloads.append(True)
            return original_reload(*args, **kwargs)
        page.goto, page.reload = goto, reload
        try:
            if args.manual_login:
                from urllib.parse import quote_plus
                domain = apply.CFG['sources'].get('indeed_domain', 'www.indeed.com')
                days = max(1, apply.CFG['search']['posted_within_hours'] // 24)
                if not is_indeed_url(page.url, domain):
                    page.goto(f'https://{domain}/jobs?q={quote_plus(report["keyword"])}&l=Remote&fromage={days}&sort=date',
                              wait_until='domcontentloaded', timeout=30000)
                else:
                    report['initial_navigation_skipped'] = True  # Do not interrupt an existing login/page.
                report['manual_login_window'] = True
                report['adapter_card_collection_tested'] = False
                # Even an initially accessible search must not close the window
                # before the user has time to sign in. A local signal permits
                # an explicit early continuation after the user says they are done.
                signal = folder / 'continue'
                print('MANUAL_CONTINUE_FILE', signal, flush=True)
                print(f'Manual window open for {args.wait_seconds} seconds. After login return to the search; no automatic navigation.', flush=True)
                deadline = time.monotonic() + args.wait_seconds
                while time.monotonic() < deadline and not signal.exists():
                    if apply.STOP.is_set() or (folder / 'stop').exists():
                        raise apply.Stopped
                    page.wait_for_timeout(1000)
                if sources.indeed_ready_evidence(page, 'search'):
                    report['cards'] = page.locator('[data-jk]:visible').evaluate_all(
                        'es=>es.slice(0,5).map(e=>({id:e.getAttribute("data-jk"),text:(e.closest("li, .job_seen_beacon, td")||e).innerText}))')
            else:
                for job, _application in itertools.islice(sources.indeed(page), 5):
                    report['cards'].append(job)
            report['ready'] = sources.indeed_ready_evidence(page, 'search')
            report['final_url'], report['title'] = diagnostic_url(page.url), page.title()
            report['blocked'] = apply.blocked(page)
            report['auth_pending'] = sources.indeed_auth_pending(page)
            page.screenshot(path=str(folder / 'page.png'))
        except Exception as e:
            report['error_type'] = type(e).__name__  # Exceptions may contain token-bearing URLs.
        finally:
            report['app_goto'], report['app_reload'] = len(navigations), len(reloads)
            ctx.close()
    report['production_files_unchanged'] = all(hashlib.sha256(p.read_bytes()).hexdigest() == h for p, h in before.items())
    (folder / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print('REPORT', folder, flush=True)
    print(json.dumps({k: v for k, v in report.items() if k not in ('observations', 'cards')}, ensure_ascii=False, indent=2), flush=True)
    print('CARDS', len(report['cards']), flush=True)


if __name__ == '__main__':
    main()
