#!/usr/bin/env python3
"""Passive cookie-policy/network diagnostics, without cookie values or tokens.

No login clicks, request interception, profile changes, retries or applications.
Only rejection reasons/categories are persisted, not Cookie/Set-Cookie contents.
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
from urllib.parse import quote_plus, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ['DRY_RUN'] = 'true'
import apply
import sources
from playwright.sync_api import sync_playwright
from tools.indeed_browser_session import reuse_page, diagnostic_url


def cookie_summary(entries):
    """Whitelist metadata; deliberately never read value or cookieLine fields."""
    rows = []
    for entry in entries:
        cookie = entry.get('cookie') or {}
        name = cookie.get('name', '')
        kind = 'clearance' if name == 'cf_clearance' else 'cloudflare' if name.startswith('__cf') else 'site_cookie'
        rows.append({'category': kind, 'blocked_reasons': list(entry.get('blockedReasons', []))})
    return rows


def policy_summary(text):
    """Keep CSP diagnostic categories, not raw messages or nonce-bearing policy."""
    return {'directives': sorted(set(re.findall(r'\b(?:default|script|frame|child|connect|style|img|worker)-src(?:-elem|-attr)?\b', text))),
            'report_only': bool(re.search(r'report.only', text, re.I)),
            'sandbox_scripts_blocked': 'sandboxed' in text.lower() and 'allow-scripts' in text.lower(),
            'unsafe_eval_mentioned': 'unsafe-eval' in text.lower()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds', type=int, default=60)
    args = parser.parse_args()
    if not 10 <= args.seconds <= 180:
        parser.error('Use 10–180 seconds')
    apply.load_settings()
    apply.apply_cv_profile(apply.SETTINGS['cv_profile'])
    apply.DRY_RUN = True
    sources.core = apply
    files = [apply.HOME / 'data' / name for name in
             ['applied.json', 'applications.csv', 'manual_apply.csv', 'user_settings.json', 'run.log']]
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in files if p.exists()}
    folder = apply.HOME / 'data/browser-diagnostics' / ('indeed-cookie-policy-' + dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    folder.mkdir(parents=True)
    report = {'cookie_values_saved': False, 'request_interception': False, 'settings_changed': False,
              'application_actions': 0, 'responses': [], 'request_cookie_metadata': [], 'response_cookie_rejections': [],
              'network_failures': [], 'policy_messages': []}
    preferences = apply.PROFILE_DIR / 'Default/Preferences'
    if preferences.exists():
        prefs = json.loads(preferences.read_text(encoding='utf-8'))
        report['profile_policy'] = {}
        for path in ('profile.block_third_party_cookies', 'profile.cookie_controls_mode',
                     'profile.default_content_setting_values.cookies', 'profile.default_content_setting_values.javascript',
                     'tracking_protection.block_all_3pc_toggle_enabled'):
            value = prefs
            for key in path.split('.'):
                value = value.get(key) if isinstance(value, dict) else None
            report['profile_policy'][path] = value if value is None or type(value) in (bool, int) else '[non-scalar]'
    domain = apply.CFG['sources'].get('indeed_domain', 'www.indeed.com')
    keyword = apply.CFG['search']['keywords'][0]
    days = max(1, apply.CFG['search']['posted_within_hours'] // 24)
    target = f'https://{domain}/jobs?q={quote_plus(keyword)}&l=Remote&fromage={days}&sort=date'
    with sync_playwright() as pw:
        ctx = apply.launch(pw)
        page, report['reused_tab'] = reuse_page(ctx, domain)
        session = ctx.new_cdp_session(page)
        request_urls = {}
        def relevant(url):
            host = (urlparse(url).hostname or '').lower()
            return any(host == suffix or host.endswith('.' + suffix) for suffix in ('indeed.com', 'cloudflare.com'))
        def request(event):
            url = event.get('request', {}).get('url', '')
            if relevant(url):
                request_urls[event['requestId']] = diagnostic_url(url)
        def request_extra(event):
            url = request_urls.get(event['requestId'])
            if url and len(report['request_cookie_metadata']) < 500:
                report['request_cookie_metadata'].append({'url_at_event': url, 'cookies': cookie_summary(event.get('associatedCookies', []))})
        def response_extra(event):
            url = request_urls.get(event['requestId'])
            if url and event.get('blockedCookies'):
                report['response_cookie_rejections'].append({'url_at_event': url, 'cookies': cookie_summary(event['blockedCookies'])})
        def failed(event):
            url = request_urls.get(event['requestId'])
            if url and len(report['network_failures']) < 100:
                report['network_failures'].append({'url_at_event': url, 'blocked_reason': event.get('blockedReason'),
                                                  'network_errors': re.findall(r'\bnet::ERR_[A-Z_]+\b', event.get('errorText', '')),
                                                  'canceled': bool(event.get('canceled'))})
        def response(resp):
            if relevant(resp.url) and len(report['responses']) < 400:
                headers = resp.headers
                report['responses'].append({'url': diagnostic_url(resp.url), 'status': resp.status, 'type': resp.request.resource_type,
                                            'headers': {key: headers[key] for key in ('cf-ray', 'cf-mitigated', 'server', 'date') if key in headers}})
        def console(message):
            if message.type in ('warning', 'error') and len(report['policy_messages']) < 100:
                summary = policy_summary(message.text)
                if summary['directives'] or summary['sandbox_scripts_blocked']:
                    report['policy_messages'].append(summary)
        session.on('Network.requestWillBeSent', request)
        session.on('Network.requestWillBeSentExtraInfo', request_extra)
        session.on('Network.responseReceivedExtraInfo', response_extra)
        session.on('Network.loadingFailed', failed)
        session.send('Network.enable')
        page.on('response', response)
        page.on('console', console)
        try:
            page.goto(target, wait_until='domcontentloaded', timeout=30000)
            print('Passive diagnosis only; no need to repeat CAPTCHA or login.', flush=True)
            deadline = time.monotonic() + args.seconds
            while time.monotonic() < deadline and not (folder / 'stop').exists():
                if apply.STOP.is_set():
                    break
                page.wait_for_timeout(1000)
            report.update(ready=sources.indeed_ready_evidence(page, 'search'), blocked=apply.blocked(page),
                          auth_pending=sources.indeed_auth_pending(page), final_url=diagnostic_url(page.url))
        except Exception as error:
            report['error_type'] = type(error).__name__
        finally:
            ctx.close()
    report['production_files_unchanged'] = all(hashlib.sha256(p.read_bytes()).hexdigest() == h for p, h in before.items())
    (folder / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print('REPORT', folder, flush=True)
    print(json.dumps({key: value for key, value in report.items() if key not in
                      ('responses', 'request_cookie_metadata', 'response_cookie_rejections', 'policy_messages')}, ensure_ascii=False, indent=2), flush=True)


if __name__ == '__main__':
    main()
