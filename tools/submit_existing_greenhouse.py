#!/usr/bin/env python3
"""One explicitly approved Greenhouse Submit without reloading user-filled fields.

macOS recovery for a stale Playwright form selector after file inputs disappear.
The owning controller is suspended so the normal durable guard is the only writer.
This tool never fills fields, answers assessments, or interacts with CAPTCHA.
"""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from urllib.parse import urlparse
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import apply
from form_validation import NATIVE_FORM_DIAGNOSTICS_JS


class Browser:
    def __init__(self, window, tab):
        self.window, self.tab = window, tab

    def js(self, expression, *, main=False):
        if main:
            code = 'document.documentElement.setAttribute("data-autoapply-recovery-result",JSON.stringify((' + expression + ')));'
            expression = '''(()=>{const s=document.createElement('script');
                const n=document.querySelector('script[nonce]');if(n)s.nonce=n.nonce;
                s.textContent=BODY;document.documentElement.appendChild(s);s.remove();
                const result=document.documentElement.getAttribute('data-autoapply-recovery-result');
                document.documentElement.removeAttribute('data-autoapply-recovery-result');return result;})()'''.replace('BODY', json.dumps(code))
        else:
            expression = 'JSON.stringify((' + expression + '))'
        source = (f'tell application "Google Chrome"\nset t to tab id {self.tab} of window id {self.window}\n'
                  f'return execute t javascript {json.dumps(expression, ensure_ascii=False)}\nend tell')
        result = subprocess.run(['osascript', '-e', source], capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError('Chrome inspection failed: ' + result.stderr.strip()[:300])
        output = result.stdout.strip()
        if output in ('', 'missing value', 'null'):
            raise RuntimeError('Browser operation could not be verified; no automatic retry')
        return json.loads(output)

    @property
    def url(self):
        return self.js('location.href')


SNAPSHOT = r'''(()=>{
    const forms=document.querySelectorAll('form#application-form');
    if(forms.length!==1) throw new Error('Expected one application form');
    const form=forms[0], visible=e=>!!e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden';
    const buttons=[...form.querySelectorAll('button[type=submit],input[type=submit]')].filter(visible);
    const elements=[...form.querySelectorAll('input,textarea,select,[aria-required=true]')];
    const issues=(DIAGNOSTICS)(elements);
    const values=JSON.stringify(elements.filter(e=>e.type!=='hidden').map(e=>
        [e.id,e.name,e.type,e.value,e.checked,[...e.querySelectorAll('option:checked')].map(o=>o.value)]))+
        [...form.querySelectorAll('.select__single-value,.select__multi-value__label,.file-upload__filename')].map(e=>e.innerText).join('|');
    let h=2166136261;for(let i=0;i<values.length;i++)h=Math.imul(h^values.charCodeAt(i),16777619);
    return {url:location.href,marker:document.documentElement.getAttribute('data-autoapply-document'),
        fingerprint:(h>>>0).toString(16),visible:visible(form),issues,
        errors:[...form.querySelectorAll('[aria-invalid=true],[role=alert]')].filter(visible).map(e=>e.innerText).filter(Boolean),
        targets:[form.action,...[...form.querySelectorAll('[formaction]')].map(e=>e.formAction)],
        buttons:buttons.map(e=>({label:(e.innerText||e.value||'').trim(),disabled:e.disabled})),
        uploads:[...form.querySelectorAll('.file-upload__filename p')].map(e=>e.innerText.trim())};
})()'''.replace('DIAGNOSTICS', NATIVE_FORM_DIAGNOSTICS_JS)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--job-id', required=True)
    parser.add_argument('--window', type=int, required=True)
    parser.add_argument('--tab', type=int, required=True)
    parser.add_argument('--controller-pid', type=int, required=True)
    parser.add_argument('--confirm-submit', action='store_true', required=True)
    args = parser.parse_args()
    apply.load_settings()
    apply.application_policy.activate(apply)
    if apply.SETTINGS.get('test_mode', True):
        raise ValueError('Saved Test mode forbids sending')
    settings = json.loads((apply.HOME / 'data/browser-diagnostics/assisted-session.json').read_text())
    request = urllib.request.Request(f"http://127.0.0.1:{settings['port']}/status", data=b'{}',
                                     headers={'Authorization': 'Bearer ' + settings['token']})
    current = json.loads(urllib.request.urlopen(request, timeout=10).read()).get('current') or {}
    if current.get('job_id') != args.job_id or current.get('kind') != 'external_trusted':
        raise ValueError('Owning controller does not have this trusted application open')
    url = current['document_url']
    if apply.ats_provider(url) != 'greenhouse':
        raise ValueError('Recovery supports only the already-verified Greenhouse application')
    key = apply.application_identity.application_key(url, apply)
    browser = Browser(args.window, args.tab)
    baseline = browser.js(SNAPSHOT)
    if (baseline['url'] != url or not baseline['marker'] or not baseline['visible'] or baseline['issues'] or baseline['errors']
            or len(baseline['buttons']) != 1 or baseline['buttons'][0]['disabled']
            or baseline['buttons'][0]['label'].casefold() != 'submit application'
            or 'CV_Andre2026.pdf' not in baseline['uploads']
            or any(apply.application_identity.application_key(target, apply) != key for target in baseline['targets'])):
        raise ValueError('Form is not ready or its identity/targets/uploads could not be verified')
    command = subprocess.run(['ps', '-p', str(args.controller_pid), '-o', 'command='], capture_output=True, text=True, check=True).stdout
    if 'tools/linkedin_assisted.py' not in command:
        raise ValueError('Refusing to suspend an unrelated process')
    os.kill(args.controller_pid, signal.SIGSTOP)
    try:
        apply.DRY_RUN = False
        apply.reset_submission_attempts()
        class Submit:
            def click(self):
                now = browser.js(SNAPSHOT)
                if (now['url'] != url or now['marker'] != baseline['marker'] or now['fingerprint'] != baseline['fingerprint']
                        or now['issues'] or now['errors'] or len(now['buttons']) != 1 or now['buttons'][0]['disabled']):
                    raise RuntimeError('Form changed after approval; no Submit performed')
                expression = '''(()=>{if(location.href!==URL||document.documentElement.getAttribute('data-autoapply-document')!==MARKER)
                    throw new Error('Document changed');const form=document.querySelector('form#application-form');
                    const b=[...form.querySelectorAll('button[type=submit],input[type=submit]')].filter(e=>e.getClientRects().length);
                    if(b.length!==1||b[0].disabled||(b[0].innerText||b[0].value||'').trim().toLowerCase()!=='submit application')
                        throw new Error('Submit changed');
                    window.__autoapplyManualClickUsed=false;window.__autoapplyManualSubmitUsed=false;
                    window.__autoapplyManualAllowSubmit=true;b[0].click();return {invoked:true};})()'''
                browser.js(expression.replace('URL', json.dumps(url)).replace('MARKER', json.dumps(baseline['marker'])), main=True)

        def wait_result():
            deadline = time.monotonic() + 40
            while time.monotonic() < deadline:
                observation = browser.js('''(()=>({url:location.href,lines:[...document.querySelectorAll('h1,h2,[role=status],[role=alert],#application_confirmation')]
                    .filter(e=>e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden').flatMap(e=>e.innerText.split(String.fromCharCode(10))),
                    invalid:[...document.querySelectorAll('[aria-invalid=true]')].some(e=>e.getClientRects().length)}))()''')
                if urlparse(observation['url']).hostname != urlparse(url).hostname:
                    return 'submission_unconfirmed'
                if any(apply.APPLICATION_SENT_RE.fullmatch(line.strip()) for line in observation['lines']):
                    return 'applied_external'
                if observation['invalid']:
                    return 'submission_rejected'
                time.sleep(.6)
            return 'submission_unconfirmed'

        status = apply.submit_application(browser, Submit(), args.job_id, current['title'], current['company'],
                                          url, wait_result, apply.load_applied())
        print(json.dumps({'result': status, 'job_id': args.job_id, 'attempts': apply.ATTEMPTS['total']}), flush=True)
    finally:
        try:
            browser.js('(()=>{window.__autoapplyManualAllowSubmit=false;return true})()', main=True)
        except Exception:
            pass
        os.kill(args.controller_pid, signal.SIGCONT)


if __name__ == '__main__':
    main()
