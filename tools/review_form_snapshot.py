#!/usr/bin/env python3
"""Read an already-open Greenhouse/Lever review without navigating or submitting.

macOS Chrome must allow JavaScript from Apple Events. This tool never changes
that preference. The snapshot contains private answers; do not publish it.
"""
import argparse
import datetime as dt
import json
import os
import re
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from form_validation import NATIVE_FORM_DIAGNOSTICS_JS


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True)
    args = parser.parse_args()
    expected = urlparse(args.url)
    greenhouse = expected.hostname in ('job-boards.greenhouse.io', 'boards.greenhouse.io') and '/jobs/' in expected.path
    lever = expected.hostname == 'jobs.lever.co' and re.fullmatch(r'/[^/]+/[0-9a-fA-F-]{36}(?:/apply)?/?', expected.path)
    if expected.scheme != 'https' or not (greenhouse or lever):
        parser.error('Expected the exact HTTPS Greenhouse/Lever job URL currently under review')
    prefix = f'https://{expected.hostname}{expected.path.rstrip("/")}'
    javascript = r'''JSON.stringify((()=>{
        const expected=EXPECTED;
        if(location.origin+location.pathname.replace(/\/$/,'')!==expected) throw new Error('Different job; nothing read');
        const form=document.querySelector('#candidate-location')?.closest('form')||document.querySelector('form');
        if(!form) throw new Error('Application form not found');
        const clean=s=>(s||'').replace(/\s+/g,' ').trim();
        const label=e=>clean((e.getAttribute('aria-labelledby')||'').split(/\s+/).map(id=>document.getElementById(id)?.textContent||'').join(' '))||
            clean([...(e.labels||[])].map(l=>l.textContent).join(' '))||clean(e.getAttribute('aria-label'));
        const elements=[...form.querySelectorAll('input,textarea,select,[aria-required="true"]')];
        const locationValue=form.querySelector('#selected-location')?.value;
        let leverLocation=null;try{leverLocation=JSON.parse(locationValue||'null')}catch{}
        return {url:location.href,title:document.title,html:form.outerHTML,leverLocation,
            captchaFrames:[...document.querySelectorAll('iframe[src*="hcaptcha"][title*="challenge"],iframe[src*="recaptcha"][title*="challenge"]')].map(e=>({
                title:e.title,visible:!!e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden'&&getComputedStyle(e).display!=='none'})),
            diagnostics:(VALIDATION)(elements),
            controls:elements.filter(e=>e.type!=='password').map(e=>({
                tag:e.tagName,id:e.id,name:e.name,type:e.type,role:e.getAttribute('role'),label:label(e),
                question:clean(e.closest('.application-question')?.querySelector('.application-label')?.textContent)||label(e),
                value:e.type==='file'?undefined:e.value,checked:e.checked,
                files:e.type==='file'?[...e.files].map(f=>({name:f.name,size:f.size})):undefined,
                selected:e.closest('.select__control')?[...e.closest('.select__control').querySelectorAll('.select__single-value,.select__multi-value__label')].map(n=>clean(n.textContent)):[],
                required:e.required,ariaRequired:e.getAttribute('aria-required'),ariaInvalid:e.getAttribute('aria-invalid'),
                nativeValid:e.validity?.valid,validationMessage:e.validationMessage
            }))};
    })())'''.replace('EXPECTED', json.dumps(prefix)).replace('VALIDATION', NATIVE_FORM_DIAGNOSTICS_JS)
    apple_script = '''on run argv
        if application "Google Chrome" is not running then error "Chrome is not running"
        tell application "Google Chrome"
            repeat with w in windows
                repeat with t in tabs of w
                    if (URL of t starts with (item 1 of argv)) then
                        return execute t javascript (item 2 of argv)
                    end if
                end repeat
            end repeat
        end tell
        error "The requested job tab was not found; nothing was opened"
    end run'''
    result = subprocess.run(['osascript', '-e', apple_script, prefix, javascript], text=True, capture_output=True, timeout=15)
    if result.returncode:
        raise SystemExit(result.stderr.strip())
    snapshot = json.loads(result.stdout)
    folder = Path(__file__).resolve().parents[1] / 'data/browser-diagnostics' / ('review-snapshot-' + dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    folder.mkdir(parents=True, mode=0o700)
    destination = folder / 'answers.json'
    destination.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding='utf-8')
    os.chmod(destination, 0o600)
    print('PRIVATE_SNAPSHOT', destination)
    print('CURRENT_VALIDATION_ISSUES', json.dumps([issue['message'] for issue in snapshot['diagnostics']], ensure_ascii=False))


if __name__ == '__main__':
    main()
