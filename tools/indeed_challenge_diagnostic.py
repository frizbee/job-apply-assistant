#!/usr/bin/env python3
"""Passive, bounded Indeed challenge diagnostics; no cookies/tokens/body dumps.

Uses the existing application Chrome profile/launch settings. Does not intercept
requests, click controls, fill forms, alter the fingerprint or bypass challenges.
Only the user may manually complete login/security verification in the window.
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
from tools.indeed_browser_session import reuse_page


def safe_url(url):
    """Drop queries and opaque challenge/auth path segments before persistence."""
    parsed = urlparse(url)
    host = parsed.hostname or ''
    path = parsed.path
    if host.endswith('cloudflare.com') or '/cdn-cgi/' in path:
        path = '/[challenge-resource]'
    elif path not in ('/', '/jobs', '/jobs/', '/favicon.ico', '/robots.txt'):
        path = '/[resource]'
    return f'{parsed.scheme}://{host}{path}' if host else '[local-document]'


def error_summary(text):
    """Keep known error categories/codes, never arbitrary console contents."""
    categories = [name for name, pattern in (
        ('csp', r'content.security.policy|refused to (?:load|frame|execute)'),
        ('network', r'failed to load|net::ERR_|networkerror'),
        ('cookie_warning', r'cookie'),
        ('challenge', r'turnstile|cloudflare|challenge'),
    ) if re.search(pattern, text, re.I)]
    codes = sorted(set(re.findall(r'\b(?:100|110|120|200|300|600)\d{3}\b', text)))
    network = sorted(set(re.findall(r'\b(?:net::)?ERR_[A-Z_]+\b', text)))
    return {'categories': categories, 'challenge_error_codes': codes, 'network_errors': network}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wait-seconds', type=int, default=120)
    args = parser.parse_args()
    if not 10 <= args.wait_seconds <= 180:
        parser.error('Use a wait of 10–180 seconds')
    apply.load_settings()
    apply.apply_cv_profile(apply.SETTINGS['cv_profile'])
    apply.DRY_RUN = True
    sources.core = apply
    keyword = apply.CFG['search']['keywords'][0]
    domain = apply.CFG['sources'].get('indeed_domain', 'www.indeed.com')
    days = max(1, apply.CFG['search']['posted_within_hours'] // 24)
    url = f'https://{domain}/jobs?q={quote_plus(keyword)}&l=Remote&fromage={days}&sort=date'
    files = [apply.HOME / 'data' / name for name in
             ['applied.json', 'applications.csv', 'manual_apply.csv', 'user_settings.json', 'run.log']]
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in files if p.exists()}
    folder = apply.HOME / 'data/browser-diagnostics' / ('indeed-challenge-' + dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    folder.mkdir(parents=True)
    report = {'keyword': keyword, 'passive_network_observation': True, 'request_interception': False,
              'launch_settings_unchanged': True, 'application_actions': 0, 'submit_calls': 0,
              'responses': [], 'request_failures': [], 'console': [], 'observations': [], 'app_goto': 0, 'app_reload': 0}
    started = time.monotonic()
    with sync_playwright() as pw:
        ctx = apply.launch(pw)
        page, report['reused_tab'] = reuse_page(ctx, domain)
        page.set_default_timeout(3000)

        def response(resp):
            if len(report['responses']) >= 300:
                return
            parsed = urlparse(resp.url)
            if not (parsed.hostname or '').endswith(('indeed.com', 'cloudflare.com')):
                return
            # response.headers excludes Set-Cookie. Only this allowlist is kept.
            headers = resp.headers
            report['responses'].append({'url': safe_url(resp.url), 'status': resp.status,
                                        'resource_type': resp.request.resource_type,
                                        'headers': {name: headers[name] for name in
                                                    ('server', 'cf-ray', 'cf-mitigated', 'content-type') if name in headers}})

        def failed(req):
            if len(report['request_failures']) < 100:
                report['request_failures'].append({'url': safe_url(req.url), 'resource_type': req.resource_type,
                                                   **error_summary(req.failure or '')})

        def console(message):
            if message.type in ('warning', 'error') and len(report['console']) < 100:
                report['console'].append({'type': message.type, **error_summary(message.text)})

        page.on('response', response)
        page.on('requestfailed', failed)
        page.on('console', console)
        page.on('pageerror', lambda error: report['console'].append({'type': 'pageerror', **error_summary(str(error))}) if len(report['console']) < 100 else None)
        try:
            report['app_goto'] += 1
            page.goto(url, wait_until='domcontentloaded', timeout=30000)
            report['browser'] = page.evaluate('''() => ({user_agent: navigator.userAgent,
                languages: navigator.languages, platform: navigator.platform,
                webdriver: navigator.webdriver, cookies_enabled: navigator.cookieEnabled,
                secure_context: isSecureContext})''')
            print('WAITING: complete Indeed verification manually if shown; no scripted clicks or reloads.', flush=True)
            deadline = time.monotonic() + args.wait_seconds
            while time.monotonic() < deadline:
                if apply.STOP.is_set():
                    report['stopped'] = True
                    break
                observation = {'seconds': round(time.monotonic() - started), 'url': safe_url(page.url),
                               'ready': sources.indeed_ready_evidence(page, 'search'),
                               'blocked': apply.blocked(page), 'auth_pending': sources.indeed_auth_pending(page),
                               'challenge_frames': []}
                for frame in page.frames:
                    if 'challenges.cloudflare.com' not in frame.url:
                        continue
                    try:
                        text = frame.locator('body').inner_text(timeout=400)
                        states = [label for label, pattern in (
                            ('human_check', r'verify you are human|verify.*human'),
                            ('success', r'verification successful|success!|you are verified'),
                            ('failure', r'verification failed|failure|unable to verify'),
                            ('expired', r'expired|timed out'),
                        ) if re.search(pattern, text, re.I)]
                        observation['challenge_frames'].append({'states': states, **error_summary(text)})
                    except Exception:
                        observation['challenge_frames'].append({'states': ['unavailable_or_reloading']})
                report['observations'].append(observation)
                if observation['ready']:
                    break
                page.wait_for_timeout(2000)
            report['final_ready'] = sources.indeed_ready_evidence(page, 'search')
            report['final_blocked'] = apply.blocked(page)
            report['final_title_is_challenge'] = bool(re.search('just a moment|verification|attention required', page.title(), re.I))
        except Exception as e:
            # Exception strings can contain full token-bearing URLs; keep type only.
            report['error_type'] = type(e).__name__
        finally:
            ctx.close()
    report['production_files_unchanged'] = all(hashlib.sha256(p.read_bytes()).hexdigest() == h for p, h in before.items())
    (folder / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print('REPORT', folder, flush=True)
    print(json.dumps({k: v for k, v in report.items() if k not in ('responses', 'observations', 'console')}, ensure_ascii=False, indent=2), flush=True)


if __name__ == '__main__':
    main()
