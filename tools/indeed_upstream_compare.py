#!/usr/bin/env python3
"""Compare upstream/current search readiness on the SAME browser/document.

Read-only, one navigation, no request interception, form filling or submissions.
Does not run upstream application code. Verifies the pinned upstream predicate
against the existing local helper before comparing both predicates in parallel.
"""
import argparse
import ast
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.parse import quote_plus

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ['DRY_RUN'] = 'true'
import apply
import sources
from playwright.sync_api import sync_playwright
from tools.indeed_browser_session import reuse_page, diagnostic_url

UPSTREAM_SHA = '7fd1748c31d395d21c52efc1eb44728344db938f'


def verify_upstream():
    url = f'https://raw.githubusercontent.com/klncgty/linkedin-auto-apply/{UPSTREAM_SHA}/sources.py'
    text = subprocess.check_output(['curl', '--fail', '--silent', '--show-error', url], text=True, timeout=30)
    upstream = next(node for node in ast.parse(text).body if isinstance(node, ast.FunctionDef) and node.name == 'page_ready')
    local = next(node for node in ast.parse((apply.HOME / 'sources.py').read_text(encoding='utf-8')).body
                 if isinstance(node, ast.FunctionDef) and node.name == 'page_ready')
    if ast.dump(upstream, include_attributes=False) != ast.dump(local, include_attributes=False):
        raise RuntimeError('Upstream page_ready differs; do not claim an equivalent comparison')
    return {'commit': UPSTREAM_SHA, 'sources_sha256': hashlib.sha256(text.encode()).hexdigest(),
            'legacy_predicate_ast_matches': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds', type=int, default=60)
    args = parser.parse_args()
    if not 10 <= args.seconds <= 600:
        parser.error('Use 10–600 seconds')
    evidence = verify_upstream()
    files = [apply.HOME / 'data' / name for name in
             ['applied.json', 'applications.csv', 'manual_apply.csv', 'user_settings.json', 'run.log']]
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in files if p.exists()}
    apply.load_settings()
    apply.apply_cv_profile(apply.SETTINGS['cv_profile'])
    apply.DRY_RUN = True
    sources.core = apply
    keyword = apply.CFG['search']['keywords'][0]
    domain = apply.CFG['sources'].get('indeed_domain', 'www.indeed.com')
    days = max(1, apply.CFG['search']['posted_within_hours'] // 24)
    url = f'https://{domain}/jobs?q={quote_plus(keyword)}&l=Remote&fromage={days}&sort=date'
    folder = apply.HOME / 'data/browser-diagnostics' / ('indeed-upstream-compare-' + dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    folder.mkdir(parents=True)
    report = {'upstream': evidence, 'scope': 'readiness predicates only; not upstream application or full runner',
              'keyword': keyword, 'same_page_and_session': True, 'request_interception': False,
              'application_actions': 0, 'submit_calls': 0, 'app_goto': 0, 'app_reload': 0,
              'responses': [], 'observations': [], 'discarded_transition_samples': 0}
    with sync_playwright() as pw:
        ctx = apply.launch(pw)
        page, report['reused_tab'] = reuse_page(ctx, domain)
        page.set_default_timeout(3000)
        def response(resp):
            if resp.request.is_navigation_request() and resp.request.frame == page.main_frame:
                report['responses'].append({'url': diagnostic_url(resp.url), 'status': resp.status})
        page.on('response', response)
        try:
            report['app_goto'] += 1
            page.goto(url, wait_until='domcontentloaded', timeout=30000)
            started, stable_url, stable_since = time.monotonic(), None, None
            disagreement_since = None
            while time.monotonic() - started < args.seconds:
                if apply.STOP.is_set() or (folder / 'continue').exists():
                    report['stopped'] = True
                    break
                now = time.monotonic()
                document = page.evaluate_handle('document')
                initial_url = page.url
                legacy = sources.page_ready(page)
                current = sources.indeed_ready_evidence(page, 'search')
                blocked = apply.blocked(page)
                auth_pending = sources.indeed_auth_pending(page)
                cards = page.locator('[data-jk]:visible').count()
                try:
                    same_document = document.evaluate('(d, url) => d === document && location.href === url', initial_url)
                except Exception:
                    same_document = False
                finally:
                    document.dispose()
                if not same_document or legacy != (not blocked):
                    # Navigation/DOM changes between Playwright reads are not
                    # disagreements between the predicates on the same state.
                    report['discarded_transition_samples'] += 1
                    stable_url = stable_since = disagreement_since = None
                    page.wait_for_timeout(250)
                    continue
                if not current:
                    stable_url = stable_since = None
                elif stable_url != page.url or stable_since is None:
                    stable_url, stable_since = page.url, now
                stable = bool(current and stable_since is not None and now - stable_since >= 1)
                row = {'seconds': round(now - started, 1), 'url': diagnostic_url(page.url),
                       'legacy_ready': legacy, 'current_evidence_ready': current, 'current_stable_ready': stable,
                       'blocked': blocked, 'auth_pending': auth_pending,
                       'visible_card_nodes': cards}
                report['observations'].append(row)
                if len(report['observations']) == 1 or row['url'] != report['observations'][-2]['url']:
                    print('STATE', json.dumps(row), flush=True)
                if stable and now - started >= 5:
                    break
                if legacy != current:
                    disagreement_since = disagreement_since or now
                    if now - disagreement_since >= 10:
                        break
                else:
                    disagreement_since = None
                page.wait_for_timeout(1000)
            last = report['observations'][-1] if report['observations'] else {}
            if last.get('current_stable_ready'):
                report['result'] = 'both_accept_search' if last.get('legacy_ready') else 'predicate_disagreement'
            elif any(row['legacy_ready'] and not row['current_evidence_ready'] for row in report['observations']):
                report['result'] = 'legacy_accepts_without_search_evidence'
            elif last.get('blocked'):
                report['result'] = 'both_blocked_before_search'
            else:
                report['result'] = 'inconclusive'
        except Exception as e:
            report['error_type'] = type(e).__name__
        finally:
            ctx.close()
    report['production_files_unchanged'] = all(hashlib.sha256(p.read_bytes()).hexdigest() == h for p, h in before.items())
    (folder / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print('REPORT', folder, flush=True)
    print(json.dumps({k: v for k, v in report.items() if k != 'observations'}, ensure_ascii=False, indent=2), flush=True)


if __name__ == '__main__':
    main()
