"""Local browser GUI for uploading a CV, reviewing details and running the bot.

Double-click the app or run `python gui.py` to start a local server and open the
default browser. The GUI provides start/stop controls and a live log.
The server binds only to this computer's loopback address (127.0.0.1).
"""
import base64
import copy
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import apply
import cv_profile
import llm_config
import llm_credentials

TOKEN = secrets.token_urlsafe(16)
STATE = {"running": False, "analyzing": False}
REVIEW_SESSION = None
# A plain Lock may be released by the worker that receives the reservation.
# It covers preparation and the whole run, not just Thread.start().
ACTIVITY_LOCK = threading.Lock()
REQUIRED = ["first_name", "last_name", "email", "phone_intl", "city", "country"]  # Commonly required application details.
SOURCE_NAMES = ["linkedin", "wellfound", "indeed", "remoteok", "himalayas", "weworkremotely"]


def busy_error() -> dict:
    return {"error": "already_running" if STATE["running"] else "busy"}


def begin_activity(flag: str | None = None) -> bool:
    if not ACTIVITY_LOCK.acquire(blocking=False):
        return False
    if STATE["running"] or STATE["analyzing"]:
        ACTIVITY_LOCK.release()
        return False
    if flag:
        if flag == "running":
            apply.STOP.clear()  # Clear the old run's cancellation before publishing this run.
        STATE[flag] = True
    return True


def finish_activity(flag: str | None = None) -> None:
    if flag:
        STATE[flag] = False
    ACTIVITY_LOCK.release()


def open_folder(path: Path) -> None:
    if sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    elif sys.platform == "win32":
        os.startfile(str(path))  # noqa: S606
    else:
        subprocess.Popen(["xdg-open", str(path)])


def resume_hash(path: Path | None = None) -> str:
    if path is None:
        if not apply.SETTINGS.get('resume'):
            raise FileNotFoundError('No CV selected')
        path = Path(apply.SETTINGS['resume'])
    return hashlib.sha256(path.read_bytes()).hexdigest()


def clear_cv_profile() -> None:
    apply.SETTINGS.pop('cv_profile', None)
    apply.SETTINGS.pop('cv_hash', None)
    apply.SETTINGS.pop('question_presets', None)
    apply.SETTINGS.pop('question_presets_cv_hash', None)
    apply.SETTINGS.pop('replace_prefilled_contacts', None)


def state() -> dict:
    s = apply.SETTINGS
    try:
        digest = resume_hash()
    except OSError:
        digest = ''
    prof, profile_error = s.get("cv_profile"), ""
    if prof is not None:
        try:
            cv_profile.validate(prof)
        except cv_profile.ProfileValidationError as e:
            prof, profile_error = None, str(e)
        if prof is not None and (not digest or s.get('cv_hash') != digest):
            prof, profile_error = None, 'profile: CV changed or cannot be verified; read it again or enter a new profile'
    llm, llm_error, has_key = None, '', False
    try:
        selected = apply.current_llm_config(require_model=False)
        _, choices = llm_config.profiles(apply.CFG)
        llm = {**selected.public(), 'available': selected.legacy_supported,
               'profiles': {name: config.public() for name, config in choices.items()}}
        has_key = bool(apply.find_key(selected.provider))
    except (llm_config.LLMConfigError, llm_credentials.CredentialError) as e:
        llm_error = str(e)
    credentials = {}
    for provider in llm_credentials.KEY_NAMES:
        try:
            credentials[provider] = llm_credentials.load(apply.HOME, provider).public()
        except llm_credentials.CredentialError:
            credentials[provider] = {'provider': provider, 'has_key': False, 'source': 'error'}
    return {
        "settings": {**{k: s.get(k) for k in ("salary", "currency", "monthly_salary_by_currency", "home_currency", "english", "notice", "locations", "sources", "test_mode", "target_technologies", "excluded_technologies", "excluded_title_words", "replace_prefilled_contacts")},
                     'question_presets': s.get('question_presets', []) if prof is not None and s.get('question_presets_cv_hash') == digest else []},
        'question_templates': apply.application_answers.TEMPLATES,
        "cv_name": Path(s["resume"]).name if s.get("resume") and Path(s["resume"]).exists() else "",
        "profile": prof,
        "profile_error": profile_error,
        "cv_hash": digest,
        "has_key": has_key,
        "llm": llm,
        "llm_error": llm_error,
        "credentials": credentials,
        "english_levels": {k: v[0] for k, v in apply.ENGLISH_LEVELS.items()},
        "default_locations": apply.CFG["search"]["locations"],
        "running": STATE["running"],
        "analyzing": STATE["analyzing"],
        "home": str(apply.HOME),
    }


def save_cv(body: dict) -> dict:
    if not begin_activity():
        return busy_error()
    try:
        return _save_cv(body)
    finally:
        finish_activity()


def _save_cv(body: dict) -> dict:
    name = re.sub(r"[^\w.\- ]", "_", Path(body["name"]).name)
    if Path(name).suffix.lower() not in (".pdf", ".docx"):
        return {"error": "pdf_docx_only"}
    folder = apply.HOME / "cv"
    folder.mkdir(exist_ok=True)
    path = folder / name
    data = base64.b64decode(body['data'], validate=True)
    digest = hashlib.sha256(data).hexdigest()
    try:
        previous = resume_hash()
    except OSError:
        previous = ''
    reset = previous != digest or (apply.SETTINGS.get('cv_profile') is not None
                                  and apply.SETTINGS.get('cv_hash') != digest)
    path.write_bytes(data)
    apply.SETTINGS["resume"] = str(path)
    if reset:
        clear_cv_profile()
    return {"ok": True, "cv_name": name, "cv_hash": digest, "profile_reset": reset}


def analyze(body: dict) -> dict:
    if not begin_activity("analyzing"):
        return busy_error()
    try:
        return _analyze(body)
    except llm_config.LLMConfigError as e:
        return {'error': 'invalid_llm_config', 'detail': str(e)}
    except llm_credentials.CredentialError as e:
        return {'error': 'invalid_key_config', 'detail': str(e)}
    finally:
        finish_activity("analyzing")


def _analyze(body: dict) -> dict:
    config = apply.current_llm_config()
    if not config.legacy_supported:
        return {'error': 'llm_not_implemented'}
    key = re.sub(r"[\s\u200b-\u200d\ufeff]", "", body.get("key") or "")  # Remove pasted whitespace/invisible characters.
    if key:
        if not re.fullmatch(r"sk-or-[A-Za-z0-9_-]{20,}", key):
            return {"error": "short_key"}  # The user may have copied the abbreviated key-list display.
        body["key"] = key
        try:
            cv_profile.request("/key", key, timeout_seconds=config.timeout_seconds)
        except cv_profile.urllib.error.HTTPError as e:
            if e.code in (401, 403):
                return {"error": "bad_key"}
            return {"error": "analyze_failed", "detail": f"OpenRouter HTTP {e.code}"}
        except Exception as e:
            return {"error": "analyze_failed", "detail": f"{e.__class__.__name__}: {str(e)[:150]}"}
        apply.save_key(body["key"].strip())
    key = key or apply.find_key(config.provider)
    if not key:
        return {"error": "no_key"}
    resume = Path(apply.SETTINGS.get("resume") or "missing")
    if not resume.is_file():
        return {"error": "no_cv"}
    digest = None
    try:
        digest = resume_hash(resume)
        if apply.SETTINGS.get('cv_hash') != digest:
            clear_cv_profile()
        prof = cv_profile.extract(resume, key, log=apply.log, max_output_tokens=config.max_output_tokens,
                                  timeout_seconds=config.timeout_seconds)
        cv_profile.validate(prof)
        if resume_hash(resume) != digest:
            clear_cv_profile()
            return {'error': 'cv_changed'}
    except Exception as e:
        try:
            current_hash = resume_hash(resume)
        except OSError:
            current_hash = None
        if not digest or current_hash != digest:
            clear_cv_profile()
            return {'error': 'cv_changed'}
        return {"error": "analyze_failed", "detail": f"{e.__class__.__name__}: {str(e)[:200]}"}
    apply.SETTINGS["cv_profile"] = prof
    apply.SETTINGS["cv_hash"] = digest
    return {"ok": True, "profile": prof, "cv_hash": digest, "cv_name": resume.name}


def start(body: dict, *, review_job_id: str | None = None) -> dict:
    global REVIEW_SESSION
    if not begin_activity("running"):
        return busy_error()
    handed_off = False
    snapshot = None
    try:
        review_row = None
        if review_job_id is not None:
            if not isinstance(review_job_id, str):
                return {'error': 'invalid_review_job'}
            review_row = next((row for row in deferred_jobs()['jobs'] if row['job_id'] == review_job_id), None)
            if review_row is None or not review_row['can_assist']:
                return {'error': 'review_unavailable'}
        snapshot = (copy.deepcopy(apply.SETTINGS), copy.deepcopy(apply.CFG), copy.deepcopy(apply.PROFILE),
                    apply.DRY_RUN, list(apply.LOG_LINES), apply.CFG.get("profile") is apply.PROFILE)
        result = _prepare_start(body)
        if "error" in result:
            return result
        accepted_resume, accepted_hash = Path(apply.SETTINGS['resume']), apply.SETTINGS['cv_hash']
        REVIEW_SESSION = apply.deferred_review.Session(review_job_id, apply.DRY_RUN, accepted_hash) if review_row else None
        session = REVIEW_SESSION

        def worker():
            try:
                if not apply.STOP.is_set() and resume_hash(accepted_resume) != accepted_hash:
                    raise RuntimeError('CV changed before the worker started; review the new CV first')
                if session is not None:
                    apply.deferred_review.run(apply, review_row, session)
                else:
                    apply.run_bot(reset_stop=False)  # Preserve Stop received while this worker was queued.
            except BaseException as e:  # The reservation must also survive STOP/browser closure.
                apply.log(f"ERROR: {e}")
                if session is not None:
                    session.finish('review_stopped; check history before retrying')
            finally:
                finish_activity("running")

        threading.Thread(target=worker, daemon=True).start()
        handed_off = True
        return {"ok": True}
    except Exception as e:
        if snapshot is not None:
            # Roll back the in-memory publication; no worker has been handed off.
            # finalize_settings may already have saved the user's validated preferences.
            settings, config, profile, dry_run, logs, aliased_profile = snapshot
            apply.SETTINGS.clear()
            apply.SETTINGS.update(settings)
            apply.CFG.clear()
            apply.CFG.update(config)
            apply.PROFILE.clear()
            apply.PROFILE.update(profile)
            if aliased_profile:
                apply.CFG["profile"] = apply.PROFILE
            apply.DRY_RUN = dry_run
            apply.LOG_LINES[:] = logs
        detail = f"{e.__class__.__name__}: {str(e)[:200]}"
        apply.log(f"Start failed: {detail}")
        return {"error": "startup_failed", "detail": detail}
    finally:
        if not handed_off:
            if REVIEW_SESSION is not None:
                REVIEW_SESSION.finish('not_started')
            finish_activity("running")


def deferred_jobs() -> dict:
    jobs = apply.deferred_review.queue(apply.load_applied(), apply.MANUAL_FILE, apply)
    for row in jobs:
        row['can_assist'] = row['job_id'].isdigit() or apply.ats_provider(row['apply_url']) is not None
    return {'jobs': jobs, 'manual_count': sum(row['category'] == 'manual' for row in jobs),
            'limit_count': sum(row['category'] == 'limit' for row in jobs)}


def review_action(body: dict) -> dict:
    if not isinstance(body, dict) or REVIEW_SESSION is None:
        return {'error': 'review_not_waiting'}
    return REVIEW_SESSION.action(body.get('action'), body.get('step'))


def deferred_external(body: dict, *, mark_sent: bool = False) -> dict:
    if not begin_activity():
        return busy_error()
    try:
        job_id = body.get('job_id') if isinstance(body, dict) else None
        row = next((r for r in deferred_jobs()['jobs'] if r['job_id'] == job_id), None)
        if row is None:
            return {'error': 'review_unavailable'}
        if mark_sent:
            history = apply.load_applied()
            if history.get(job_id, {}).get('status') not in apply.deferred_review.REVIEWABLE:
                return {'error': 'review_unavailable'}
            # Back up before resolving a deferred item into a protected state.
            apply.job_rechecks.backup([apply.APPLIED_FILE, apply.LOG_FILE, apply.MANUAL_FILE], apply.HOME / 'data' / 'backups')
            apply.record_manual(job_id, row['title'], row['company'], row['apply_url'], 'User reported sending outside the app; outcome not independently confirmed')
            apply.record(history, job_id, row['title'], row['company'], 'submission_unconfirmed', row['job_url'])
        else:
            if not apply.safe_https_url(row['apply_url']):
                return {'error': 'review_unavailable'}
            webbrowser.open(row['apply_url'])
        return {'ok': True}
    finally:
        finish_activity()


def _prepare_start(body: dict) -> dict:
    s = apply.SETTINGS
    if not Path(s.get("resume") or "missing").is_file():
        return {"error": "no_cv"}
    if not isinstance(body, dict) or not isinstance(body.get("profile"), dict):
        return {"error": "invalid_profile", "detail": "profile: expected object"}
    digest = resume_hash()
    if body.get('cv_hash') != digest:
        return {'error': 'cv_changed'}
    cached = s.get('cv_profile') if s.get('cv_hash') == digest else None
    prof = {**cv_profile.empty_profile(), **(cached if cv_profile.valid(cached) else {}), **body["profile"]}
    # New UI supplies separately reviewed phone components. Older clients can
    # still provide phone_intl; no calling code is guessed from an old number.
    if 'phone_country_code' in body or 'phone_national' in body:
        from phone_contact import join_phone
        try:
            prof['phone_intl'] = join_phone(body.get('phone_country_code'), body.get('phone_national'))
        except ValueError as e:
            return {'error': 'invalid_profile', 'detail': str(e)}
    # The HTML experience input is text; only this transport field is normalized.
    years = prof["total_years"]
    if isinstance(years, str):
        if not years.strip():
            prof["total_years"] = 0
        elif re.fullmatch(r"\d{1,2}", years.strip()):
            prof["total_years"] = int(years.strip())
    if prof.get("job_titles") == []:
        return {"error": "no_titles"}
    try:
        cv_profile.validate(prof)
    except cv_profile.ProfileValidationError as e:
        return {"error": "invalid_profile", "detail": str(e)}
    missing = [k for k in REQUIRED if not str(prof.get(k) or "").strip()]
    if missing:
        return {"error": "missing", "fields": missing}
    # Validate publication-critical options before changing any shared settings.
    english, locations, sources = body.get("english"), body.get("locations"), body.get("sources")
    if not isinstance(english, str) or english not in apply.ENGLISH_LEVELS:
        return {"error": "invalid_settings", "detail": "settings.english: unknown level"}
    if type(body.get("test_mode")) is not bool:
        return {"error": "invalid_settings", "detail": "settings.test_mode: expected boolean"}
    if not isinstance(sources, list) or any(not isinstance(name, str) or name not in SOURCE_NAMES for name in sources):
        return {"error": "invalid_settings", "detail": "settings.sources: expected known source names"}
    if not isinstance(locations, list) or not locations or any(not isinstance(loc, str) or not loc.strip() for loc in locations):
        return {"error": "invalid_settings", "detail": "settings.locations: expected nonblank strings"}
    technology_settings = {field: body.get(field, s.get(field, [])) for field in apply.job_relevance.USER_FIELDS}
    replace_contacts = body.get('replace_prefilled_contacts', False)
    if type(replace_contacts) is not bool:
        return {'error': 'invalid_settings', 'detail': 'replace_prefilled_contacts: expected boolean'}
    presets = body.get('question_presets', s.get('question_presets', []) if s.get('question_presets_cv_hash') == digest else [])
    try:
        apply.job_relevance.validate_settings(technology_settings)
        apply.application_answers.validate_presets(presets)
    except ValueError as e:
        return {'error': 'invalid_settings', 'detail': str(e)}
    # Monthly expectations in the user's currency, EUR and USD; annual salary is the FX base.
    cur = body.get("currency")
    if not isinstance(cur, str) or cur not in apply.CURRENCY_RE:
        return {"error": "invalid_settings", "detail": "settings.currency: unknown currency"}
    monthly = {}
    for c, v in ((cur, body.get("salary")), ("EUR", body.get("salary_eur")), ("USD", body.get("salary_usd"))):
        if v is None or (isinstance(v, str) and not v.strip()):
            continue
        number = apply.positive_decimal(v)
        if number is None:
            return {"error": "invalid_settings", "detail": "settings.salary: expected a positive amount with a decimal point (or blank)"}
        text = apply.decimal_text(number)
        if c in monthly and monthly[c] != text:
            return {"error": "invalid_settings", "detail": "settings.salary: conflicting amounts for the same currency"}
        monthly[c] = text
    # Clearing all fields must clear the old expectation, not silently reuse it.
    if resume_hash() != digest:
        return {'error': 'cv_changed'}
    base = next((c for c in ("USD", "EUR", cur) if c in monthly), cur)
    s.update(monthly_salary_by_currency=monthly, home_currency=cur, currency=base,
             salary=apply.decimal_text(apply.positive_decimal(monthly[base]) * 12) if monthly else "")
    s.update(cv_profile=prof, cv_hash=digest,
             english=body["english"], notice=re.sub(r"\D", "", str(body["notice"])) or s["notice"],
              locations=list(body["locations"]),
               sources=body["sources"], test_mode=body["test_mode"])
    s.update({field: [term.strip() for term in terms] for field, terms in technology_settings.items()})
    s.update(question_presets=[dict(entry) for entry in presets], question_presets_cv_hash=digest)
    s['replace_prefilled_contacts'] = replace_contacts
    for name in SOURCE_NAMES:
        apply.CFG["sources"][name] = name in body["sources"]
    apply.DRY_RUN = s["test_mode"]
    apply.finalize_settings()
    apply.LOG_LINES.clear()

    return {"ok": True}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/":
            return self.send(200, PAGE.replace("__TOKEN__", TOKEN).encode(), "text/html; charset=utf-8")
        if self.headers.get("X-Token") != TOKEN:
            return self.send(403, b"{}")
        if self.path == "/api/state":
            return self.send(200, json.dumps(state()).encode())
        if self.path == '/api/deferred':
            return self.send(200, json.dumps(deferred_jobs()).encode())
        m = re.match(r"/api/log\?from=(\d+)", self.path)
        if m:
            i = int(m.group(1))
            return self.send(200, json.dumps({"lines": apply.LOG_LINES[i:], "running": STATE["running"],
                                            'review': REVIEW_SESSION.public() if REVIEW_SESSION else None}).encode())
        self.send(404, b"{}")

    def do_POST(self):
        if self.headers.get("X-Token") != TOKEN:
            return self.send(403, b"{}")
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        routes = {
            "/api/cv": lambda: save_cv(body),
            "/api/analyze": lambda: analyze(body),
            "/api/start": lambda: start(body),
            '/api/review/start': lambda: start(body, review_job_id=body.get('job_id', '') if isinstance(body, dict) else ''),
            '/api/review/action': lambda: review_action(body),
            '/api/deferred/open': lambda: deferred_external(body),
            '/api/deferred/mark-sent': lambda: deferred_external(body, mark_sent=True),
            "/api/stop": lambda: (apply.STOP.set(), {"ok": True})[1],
            "/api/open": lambda: (open_folder(apply.HOME / body.get("folder", "data")), {"ok": True})[1],
            "/api/quit": lambda: (apply.STOP.set(), threading.Timer(0.5, lambda: os._exit(0)).start(), {"ok": True})[2],
        }
        if self.path not in routes:
            return self.send(404, b"{}")
        self.send(200, json.dumps(routes[self.path]()).encode())


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Auto Apply</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root { --bg:#f6f7f9; --card:#fff; --text:#1d2433; --muted:#667085; --line:#e4e7ec; --accent:#2563eb; --ok:#16a34a; --warn:#b45309; }
* { box-sizing: border-box; }
body { margin:0; font:15px/1.5 -apple-system, "Segoe UI", Roboto, sans-serif; background:var(--bg); color:var(--text); }
main { max-width: 820px; margin: 0 auto; padding: 28px 20px 60px; }
header { display:flex; align-items:center; justify-content:space-between; margin-bottom: 18px; }
h1 { font-size: 24px; margin: 0; }
h2 { font-size: 17px; margin: 0 0 4px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:20px; margin-bottom:16px; }
.step { color:var(--muted); font-size:13px; text-transform:uppercase; letter-spacing:.04em; }
p.hint { color:var(--muted); margin: 4px 0 14px; }
label { display:block; font-weight:600; font-size:13px; margin: 12px 0 4px; }
input[type=text], input[type=password], select, textarea { width:100%; padding:9px 11px; border:1px solid var(--line); border-radius:8px; font:inherit; background:#fff; }
textarea { min-height: 90px; resize: vertical; }
.grid { display:grid; grid-template-columns: 1fr 1fr; gap: 0 14px; }
.grid3 { display:grid; grid-template-columns: 1fr 1fr 1fr; gap: 0 14px; }
button { font:inherit; font-weight:600; border:0; border-radius:8px; padding:10px 16px; cursor:pointer; background:var(--accent); color:#fff; }
button.secondary { background:#eef2ff; color:var(--accent); }
button.danger { background:#dc2626; }
button:disabled { opacity:.5; cursor:default; }
.row { display:flex; gap:10px; align-items:center; flex-wrap:wrap; margin-top:14px; }
.drop { border:2px dashed var(--line); border-radius:12px; padding:22px; text-align:center; cursor:pointer; }
.drop.has { border-color: var(--ok); }
.checks label { display:inline-flex; align-items:center; gap:6px; font-weight:500; margin:6px 16px 0 0; }
.msg { margin-top:10px; font-size:14px; }
.msg.err { color:#b91c1c; } .msg.ok { color:var(--ok); }
#log { background:#0f172a; color:#e2e8f0; font:12.5px/1.45 ui-monospace, Menlo, Consolas, monospace; border-radius:8px; padding:12px; height:340px; overflow:auto; white-space:pre-wrap; }
.notice { background:#fffbeb; border:1px solid #fde68a; color:#78350f; border-radius:8px; padding:10px 12px; margin-top:12px; font-size:14px; }
.notice.free { background:#f0fdf4; border-color:#bbf7d0; color:#14532d; margin:14px 0 4px; }
ol.steps { margin:4px 0; padding-left:20px; color:var(--text); } ol.steps li { margin:3px 0; }
a.btn { display:inline-block; text-decoration:none; font-weight:600; border-radius:8px; padding:9px 14px; background:#eef2ff; color:var(--accent); }
.lang button { background:none; color:var(--muted); padding:4px 6px; }
.lang button.on { color:var(--text); text-decoration: underline; }
.hidden { display:none; }
input.missing { border-color:#dc2626; background:#fef2f2; }
@media (max-width: 640px) { .grid, .grid3 { grid-template-columns: 1fr; } }
</style></head>
<body><main>
<header><h1>Auto Apply</h1>
<div class="lang"><button data-l="en">EN</button><button data-l="tr">TR</button><button id="quit" data-t="quit"></button></div></header>

<section class="card" id="s1">
  <div class="step" data-t="step1"></div><h2 data-t="cv_title"></h2><p class="hint" data-t="cv_hint"></p>
  <div class="drop" id="drop"><span id="cvname" data-t="cv_drop"></span><input type="file" id="file" accept=".pdf,.docx" class="hidden"></div>
  <div id="keybox" class="hidden">
    <div class="notice free"><b data-t="free_title"></b><br><span data-t="free_text"></span></div>
    <label data-t="key_label"></label>
    <ol class="steps"><li data-t="key_s1"></li><li data-t="key_s2"></li><li data-t="key_s3"></li></ol>
    <div class="row" style="margin-top:6px"><a class="btn secondary" href="https://openrouter.ai/settings/keys" target="_blank" data-t="key_open"></a></div>
    <input type="password" id="key" placeholder="sk-or-v1-..." style="margin-top:10px">
  </div>
  <div class="row"><button id="analyze" data-t="analyze"></button><button class="secondary" id="manual" data-t="manual"></button>
    <button class="secondary hidden" id="changekey" data-t="change_key"></button></div>
  <div class="msg" id="m1"></div>
</section>

<section class="card hidden" id="s2">
  <div class="step" data-t="step2"></div><h2 data-t="review_title"></h2><p class="hint" data-t="review_hint"></p>
  <div class="grid">
    <div><label data-t="first_name"></label><input type="text" id="first_name"></div>
    <div><label data-t="last_name"></label><input type="text" id="last_name"></div>
    <div><label data-t="email"></label><input type="text" id="email"></div>
    <div><label data-t="phone_country_code"></label><input type="tel" id="phone_country_code" placeholder="+90"></div>
    <div><label data-t="phone_national"></label><input type="tel" id="phone_national" placeholder="555 000 00 00"></div>
    <div><label data-t="phone_saved"></label><input type="text" id="phone_intl" readonly><p class="hint" data-t="phone_split_hint"></p></div>
    <div><label data-t="city"></label><input type="text" id="city"></div>
    <div><label data-t="country"></label><input type="text" id="country"></div>
    <div><label>LinkedIn</label><input type="text" id="linkedin"></div>
    <div><label data-t="github"></label><input type="text" id="github"></div>
  </div>
  <div class="checks"><label><input type="checkbox" id="replace_prefilled_contacts"> <span data-t="replace_prefilled_contacts"></span></label></div>
  <label data-t="titles"></label><textarea id="job_titles"></textarea>
  <label data-t="excluded_title_words"></label><textarea id="excluded_title_words" style="min-height:60px"></textarea>
  <p class="hint" data-t="excluded_title_hint"></p>
  <label data-t="target_technologies"></label><textarea id="target_technologies" style="min-height:60px"></textarea>
  <label data-t="excluded_technologies"></label><textarea id="excluded_technologies" style="min-height:60px"></textarea>
  <p class="hint" data-t="technology_hint"></p>
  <label data-t="locations"></label><textarea id="locations" style="min-height:60px"></textarea>
  <div class="grid3">
    <div><label data-t="years"></label><input type="text" id="total_years"></div>
    <div><label data-t="notice"></label><input type="text" id="notice"></div>
  </div>
  <div class="grid3">
    <div><label data-t="salary"></label><div style="display:flex;gap:6px"><input type="text" id="salary">
      <select id="currency" style="width:90px"><option>TRY</option><option>USD</option><option>EUR</option><option>GBP</option><option>PLN</option><option>CHF</option><option>INR</option><option>CAD</option></select></div></div>
    <div><label data-t="salary_eur"></label><input type="text" id="salary_eur" placeholder="EUR"></div>
    <div><label data-t="salary_usd"></label><input type="text" id="salary_usd" placeholder="USD"></div>
  </div>
  <p class="hint" data-t="salary_hint" style="margin:6px 0 0"></p>
  <label data-t="english"></label><select id="english"></select>
  <label data-t="cover"></label><textarea id="cover_letter"></textarea>
  <h3 data-t="question_presets_title"></h3><p class="hint" data-t="question_presets_hint"></p>
  <div id="question_presets"></div>
</section>

<section class="card hidden" id="s3">
  <div class="step" data-t="step3"></div><h2 data-t="where_title"></h2><p class="hint" data-t="where_hint"></p>
  <div class="checks" id="sources"></div>
  <div class="checks"><label><input type="checkbox" id="test_mode"> <span data-t="test_mode"></span></label></div>
  <p class="hint" id="test_hint" style="margin:-4px 0 12px"></p>
  <div class="notice" data-t="chrome_notice"></div>
  <div class="row"><button id="start" data-t="start"></button><button class="danger hidden" id="stop" data-t="stop"></button>
    <button class="secondary" id="results" data-t="results"></button></div>
  <div class="msg" id="m3"></div>
</section>

<section class="card hidden" id="s4">
  <h2 data-t="progress"></h2><div id="log"></div>
</section>
<section class="card" id="deferred">
  <h2 data-t="deferred_title"></h2><p class="hint" data-t="deferred_hint"></p>
  <button class="secondary" id="refresh_deferred" data-t="refresh_deferred"></button>
  <div id="deferred_jobs"></div>
  <div id="review_panel" class="hidden">
    <p id="review_status"></p><pre id="review_issues" style="white-space:pre-wrap"></pre>
    <p class="hint" data-t="review_browser_hint"></p>
    <button id="review_continue" data-t="review_continue"></button>
    <button class="secondary" id="review_skip" data-t="review_skip"></button>
  </div>
  <div class="msg" id="review_message"></div>
</section>
</main>
<script>
const TOKEN = "__TOKEN__";
const T = {
 en: { step1:"Step 1", cv_title:"Your CV", cv_hint:"Upload your CV (PDF or Word). It is read by a free AI model to fill in your details and pick matching jobs.",
  cv_drop:"Click to choose your CV (.pdf or .docx)", key_label:"Get your free key (one time, about 1 minute)",
  free_title:"100% free - you will not pay anything.", free_text:"Your CV is read by a free AI model on OpenRouter. The app only ever uses free models. No credit card needed, nothing to buy.",
  key_s1:"Click the button below and sign in to OpenRouter (Google sign-in is fine).", key_s2:"Click \"Create\", give it any name, leave the other fields as they are, and create the key.",
  key_s3:"Click \"Copy\" in the window that shows your new key (it is shown only once) and paste it below. Then click \"Read my CV\".", key_open:"Open OpenRouter",
  analyze:"Read my CV", manual:"Skip, I'll fill it in myself", analyzing:"Reading your CV... this can take up to a minute.",
  analyzed:"Done. Please check your details below.", step2:"Step 2", review_title:"Check your details", review_hint:"These are used to search jobs and fill application forms. Fix anything that is wrong.",
  first_name:"First name", last_name:"Last name", email:"E-mail", phone:"Phone (with country code)", phone_country_code:"Phone country calling code", phone_national:"Phone number (without country calling code)", phone_saved:"Previously saved full phone number", phone_split_hint:"Confirm the calling code and remaining number separately. An unsplit old number is shown for reference only; no code is guessed from your location.", city:"City, Country", country:"Country", github:"GitHub / portfolio",
  question_presets_title:"Common questions: 10 reviewed answer templates", question_presets_hint:"Edit each question to match the employer's wording and enter your own confirmed answer (Yes/No for binary questions). Blank answers remain manual. Matching is exact apart from case, whitespace and the required-field asterisk. Country/company-specific questions are not generalized. Saved with this CV; no automatic Yes answers.",
  replace_prefilled_contacts:"Replace site-prefilled name, email and phone with my reviewed profile values when they differ. Check your profile carefully before enabling.",
  err_review_unavailable:"The deferred queue or selected application is unavailable.", err_invalid_review_job:"Choose a deferred application.", err_review_not_waiting:"This review step has changed. Wait for the current step.", err_invalid_review_action:"Unknown review action.",
  deferred_manual:"Needs manual input", deferred_limits:"Not processed: run limits", review_retry:"Process this application",
  deferred_title:"Deferred applications", deferred_hint:"Incomplete forms do not stop bulk processing. Review them one at a time afterwards. The current CV/profile and Test mode checkbox above apply to assisted review. A website draft is not guaranteed to survive reopening.", refresh_deferred:"Refresh queue", review_open:"Review this application", review_external:"Open website manually", review_mark_sent:"I submitted outside the app", review_continue:"I checked this step — check and continue", review_skip:"Skip for now", review_browser_hint:"Fill the current step in Chrome, then confirm here. Do not click Submit in Chrome. Your confirmation applies only to this step; it does not save general answers. LIVE review asks for a separate final confirmation.", review_live_confirm:"LIVE review may send this one application after your final confirmation. Continue?", review_external_confirm:"This opens your regular browser outside Test mode. Any sending there is your own manual action. Continue?", review_sent_confirm:"Did you submit this application outside the app? It will be marked UNCONFIRMED and protected against retries, not counted as confirmed success.", review_unavailable:"This item is no longer available for review.",
  titles:"Job titles to apply for (one per line)", locations:"Countries / regions to search (one per line)", years:"Years of experience", salary:"Monthly salary expectation (your currency)", salary_eur:"Monthly salary expectation in EUR", salary_usd:"Monthly salary expectation in USD",
  target_technologies:"Target technologies (one per line; advisory)", excluded_technologies:"Excluded technologies (one per line)", technology_hint:"Missing target technology evidence does not block application processing. Explicit exclusions still apply to the role title or mandatory requirements, not nice-to-have skills. Terms are literal, not regex. No automatic aliases or technology lists.",
  excluded_title_words:"Excluded words / phrases in job titles (one per line)", excluded_title_hint:"Optional; empty means no exclusions. Matches only the job title, not the description. Literal, case-insensitive terms, not regex; no built-in list.",
  notice:"Notice period (days)", english:"English level", cover:"Short cover letter (used when a form asks for one)",
   salary_hint:"Monthly amounts; use a decimal point, no grouping separators. Leave blank if unknown. Automatic answers require an explicit currency and period, and valid exchange rates when converting. Hourly/daily conversions use 2080 hours/220 days per year. Otherwise manual review is required.",
  step3:"Step 3", where_title:"Where to apply", where_hint:"Only remote jobs posted in the last 7 days. Company websites are limited to a few per run so you are not flagged as spam.",
  test_mode:"Test mode (fill in forms but do NOT send them)",
  test_off:"Test mode is OFF: real applications will be sent. Tick the box if you only want to try the app first.",
  test_on:"Test mode is ON: this is only a test run. Forms are filled in but NO application is sent. Untick the box to really apply.",
  chrome_notice:"A Chrome window will open. The first time, log in to LinkedIn (and Indeed / Wellfound if selected) in that window, or tick a 'verify you are human' box if asked. The bot waits for you and remembers the login.",
  start:"Start applying", stop:"Stop", results:"Open results folder", progress:"Progress", running:"Running...", finished:"Finished. See the results folder for the full list.",
   err_invalid_llm_config:"Invalid LLM configuration.", err_invalid_key_config:"Cannot load the selected API key.", err_llm_not_implemented:"The selected LLM adapter is not implemented yet. Use the free profile or enter your details manually.",
   cv_replaced:"CV contents changed. Read it again or enter and check a new profile.", err_cv_changed:"The CV changed. Review the current file and enter or read your profile again.",
   err_no_cv:"Please choose your CV first.", err_no_key:"Please paste your free OpenRouter key above, or skip and fill in your details yourself.", err_bad_key:"This key doesn't work. Copy it again from OpenRouter and paste the whole key.",
  err_short_key:"This looks like the shortened key from OpenRouter's list (with '...'). OpenRouter shows the full key only once: click Create to make a new key, then click Copy in the window that appears and paste it here.", err_pdf_docx_only:"Only .pdf or .docx files.",
  change_key:"Change key", quit:"Quit", quit_done:"The app is closed. You can close this tab.",
  levels:{1:"Native / bilingual", 2:"Fluent / professional", 3:"Conversational / intermediate", 4:"Basic"},
   err_analyze_failed:"Could not read the CV automatically. You can fill in your details yourself.", err_invalid_profile:"Invalid profile. Check your details or read the CV again.", err_invalid_settings:"Invalid run settings. Check the selected options.", err_startup_failed:"Could not start. Check the log and try again.", err_busy:"Another operation is in progress. Wait for it to finish.", err_no_titles:"Please enter at least one job title.", err_missing:"Please fill in the fields marked in red. Application forms always ask for them.", err_already_running:"Already running." },
 tr: { step1:"Adım 1", cv_title:"CV'n", cv_hint:"CV'ni yükle (PDF veya Word). Ücretsiz bir yapay zeka modeli okuyup bilgilerini ve sana uygun pozisyonları çıkarır.",
  cv_drop:"CV'ni seçmek için tıkla (.pdf veya .docx)", key_label:"Ücretsiz anahtarını al (bir kerelik, yaklaşık 1 dakika)",
  free_title:"Tamamen ücretsiz - hiçbir ücret ödemezsin.", free_text:"CV'n OpenRouter'daki ücretsiz bir yapay zeka modeliyle okunur. Uygulama sadece ücretsiz modelleri kullanır. Kredi kartı gerekmez, hiçbir şey satın almazsın.",
  key_s1:"Aşağıdaki butona tıkla ve OpenRouter'a giriş yap (Google ile giriş olur).", key_s2:"\"Create\" butonuna bas, istediğin bir isim ver, diğer alanlara dokunmadan anahtarı oluştur.",
  key_s3:"Yeni anahtarın gösterildiği penceredeki \"Copy\" butonuna bas (sadece bir kez gösterilir) ve aşağıya yapıştır. Sonra \"CV'mi oku\" butonuna bas.", key_open:"OpenRouter'ı aç",
  analyze:"CV'mi oku", manual:"Geç, kendim dolduracağım", analyzing:"CV okunuyor... bir dakika kadar sürebilir.",
  analyzed:"Tamam. Aşağıdaki bilgileri kontrol et.", step2:"Adım 2", review_title:"Bilgilerini kontrol et", review_hint:"İş aramak ve başvuru formlarını doldurmak için kullanılır. Yanlış olanı düzelt.",
  first_name:"Ad", last_name:"Soyad", email:"E-posta", phone:"Telefon (ülke koduyla)", phone_country_code:"Telefon ülke arama kodu", phone_national:"Telefon numarası (ülke arama kodu olmadan)", phone_saved:"Önceden kaydedilmiş tam telefon numarası", phone_split_hint:"Arama kodunu ve kalan numarayı ayrı ayrı doğrulayın. Eski numara yalnızca referans içindir; konumunuzdan kod tahmin edilmez.", city:"Şehir, Ülke", country:"Ülke", github:"GitHub / portfolyo",
  question_presets_title:"Sık sorulan sorular: 10 onaylı yanıt şablonu", question_presets_hint:"Soruyu işverenin metnine göre düzenleyin ve onayladığınız yanıtı girin (ikili sorular için Yes/No). Boş yanıtlar manuel kalır. Eşleşme yalnızca harf boyutu, boşluk ve zorunlu alan yıldızını yok sayar. Ülkeye/şirkete özel sorular genelleştirilmez. Bu CV ile kaydedilir; otomatik Yes yanıtı yoktur.",
  replace_prefilled_contacts:"Sitenin önceden doldurduğu ad, e-posta ve telefonu farklıysa onayladığım profil değerleriyle değiştir. Etkinleştirmeden önce profilinizi dikkatlice kontrol edin.",
  err_review_unavailable:"Ertelenen kuyruk veya seçili başvuru kullanılamıyor.", err_invalid_review_job:"Ertelenen bir başvuru seçin.", err_review_not_waiting:"İnceleme adımı değişti. Geçerli adımı bekleyin.", err_invalid_review_action:"Bilinmeyen inceleme işlemi.",
  deferred_manual:"Manuel giriş gerekiyor", deferred_limits:"İşlenmedi: çalışma limitleri", review_retry:"Bu başvuruyu işle",
  deferred_title:"Ertelenen başvurular", deferred_hint:"Eksik formlar toplu işlemi durdurmaz. Daha sonra tek tek inceleyin. Yukarıdaki güncel CV/profil ve Test modu seçimi geçerlidir. Site taslağının korunması garanti edilmez.", refresh_deferred:"Kuyruğu yenile", review_open:"Başvuruyu incele", review_external:"Siteyi manuel aç", review_mark_sent:"Uygulama dışında gönderdim", review_continue:"Bu adımı kontrol ettim — kontrol et ve devam et", review_skip:"Şimdilik atla", review_browser_hint:"Geçerli adımı Chrome'da doldurun ve burada onaylayın. Chrome'da Submit'e basmayın. Onay yalnızca bu adıma uygulanır; genel yanıt kaydetmez. CANLI modda ayrıca son onay istenir.", review_live_confirm:"CANLI inceleme, son onayınızdan sonra bu başvuruyu gönderebilir. Devam edilsin mi?", review_external_confirm:"Normal tarayıcınız Test modu dışında açılır. Buradaki gönderimler sizin manuel işleminizdir. Devam edilsin mi?", review_sent_confirm:"Bu başvuruyu uygulama dışında gönderdiniz mi? Doğrulanmamış olarak işaretlenecek ve tekrar denemeye karşı korunacaktır; doğrulanmış başarı sayılmaz.", review_unavailable:"Bu başvuru artık incelemeye uygun değil.",
  titles:"Başvurulacak pozisyonlar (her satıra bir tane)", locations:"Aranacak ülkeler / bölgeler (her satıra bir tane)", years:"Deneyim (yıl)", salary:"Aylık maaş beklentisi (kendi para birimin)", salary_eur:"Aylık maaş beklentisi (EUR)", salary_usd:"Aylık maaş beklentisi (USD)",
  target_technologies:"Hedef teknolojiler (her satıra bir tane; bilgi amaçlı)", excluded_technologies:"Hariç tutulan teknolojiler (her satıra bir tane)", technology_hint:"Hedef teknoloji kanıtının bulunmaması başvurunun işlenmesini engellemez. Açık hariç tutmalar pozisyon başlığına veya zorunlu gereksinimlere uygulanır; isteğe bağlı becerilere uygulanmaz. Terimler regex değildir; otomatik teknoloji listesi veya takma ad eklenmez.",
  excluded_title_words:"Pozisyon başlığında hariç tutulan kelimeler / ifadeler (her satıra bir tane)", excluded_title_hint:"İsteğe bağlı; boş liste hariç tutmaz. Yalnızca başlıkta aranır, açıklamada değil. Büyük/küçük harfe duyarsızdır; regex veya yerleşik liste değildir.",
  notice:"İhbar süresi (gün)", english:"İngilizce seviyesi", cover:"Kısa ön yazı (form isterse kullanılır)",
   salary_hint:"Aylık tutarlar; ondalık nokta kullan, binlik ayırıcı kullanma. Bilinmiyorsa boş bırak. Otomatik yanıt için açık para birimi/dönem ve dönüşümde geçerli kur gerekir. Saatlik/günlük dönüşüm yılda 2080 saat/220 gün kullanır. Aksi durumda manuel kontrol gerekir.",
  step3:"Adım 3", where_title:"Nereye başvurulsun", where_hint:"Sadece son 7 günde yayınlanan remote ilanlar. Şirket sitelerine her çalıştırmada az sayıda başvurulur, spam sayılmamak için.",
  test_mode:"Test modu (formları doldurur ama GÖNDERMEZ)",
  test_off:"Test modu KAPALI: gerçek başvurular gönderilir. Önce sadece denemek istersen kutuyu işaretle.",
  test_on:"Test modu AÇIK: bu sadece bir deneme. Formlar doldurulur ama HİÇBİR başvuru gönderilmez. Gerçekten başvurmak için işareti kaldır.",
  chrome_notice:"Bir Chrome penceresi açılacak. İlk seferde o pencerede LinkedIn'e (seçtiysen Indeed / Wellfound'a da) giriş yap; 'insan mısın' kutusu çıkarsa işaretle. Bot seni bekler ve girişi hatırlar.",
  start:"Başvurmaya başla", stop:"Durdur", results:"Sonuç klasörünü aç", progress:"İlerleme", running:"Çalışıyor...", finished:"Bitti. Tam liste sonuç klasöründe.",
   err_invalid_llm_config:"Geçersiz LLM ayarları.", err_invalid_key_config:"Seçilen API anahtarı okunamadı.", err_llm_not_implemented:"Seçilen LLM bağlantısı henüz uygulanmadı. Ücretsiz profili kullan veya bilgilerini elle gir.",
   cv_replaced:"CV içeriği değişti. Yeniden oku veya yeni profilini doldurup kontrol et.", err_cv_changed:"CV değişti. Güncel dosyayı kontrol edip profilini yeniden doldur veya oku.",
   err_no_cv:"Önce CV'ni seç.", err_no_key:"Yukarıdaki kutuya ücretsiz OpenRouter anahtarını yapıştır ya da geçip bilgilerini kendin doldur.", err_bad_key:"Bu anahtar çalışmıyor. OpenRouter'dan tekrar kopyalayıp anahtarın tamamını yapıştır.",
  err_short_key:"Bu, OpenRouter listesindeki kısaltılmış anahtar gibi görünüyor ('...' içeriyor). OpenRouter tam anahtarı sadece bir kez gösterir: Create ile yeni anahtar oluştur, açılan penceredeki Copy butonuna basıp buraya yapıştır.", err_pdf_docx_only:"Sadece .pdf veya .docx.",
  change_key:"Anahtarı değiştir", quit:"Kapat", quit_done:"Uygulama kapandı. Bu sekmeyi kapatabilirsin.",
  levels:{1:"Ana dil / iki dilli", 2:"Akıcı / profesyonel", 3:"Orta / konuşma", 4:"Temel"},
   err_analyze_failed:"CV otomatik okunamadı. Bilgilerini kendin doldurabilirsin.", err_invalid_profile:"Geçersiz profil. Bilgilerini kontrol et veya CV'yi yeniden oku.", err_invalid_settings:"Geçersiz çalışma ayarları. Seçenekleri kontrol et.", err_startup_failed:"Başlatılamadı. Kaydı kontrol edip tekrar dene.", err_busy:"Başka bir işlem devam ediyor. Tamamlanmasını bekle.", err_no_titles:"En az bir pozisyon yaz.", err_missing:"Kırmızı işaretli alanları doldur. Başvuru formları bunları her zaman soruyor.", err_already_running:"Zaten çalışıyor." },
};
const SOURCES = { linkedin:"LinkedIn (Easy Apply)", wellfound:"Wellfound", indeed:"Indeed", remoteok:"Remote OK", himalayas:"Himalayas", weworkremotely:"We Work Remotely" };
const validLanguage = value => Object.prototype.hasOwnProperty.call(T, value);
let lang = "en", S = null, logFrom = 0, poll = null, uiClosed = false, activeReview = null;
try {
  const saved = localStorage.getItem("lang");
  if (validLanguage(saved)) lang = saved;
} catch (_) {} // Storage may be disabled; the interface must still work.
const $ = id => document.getElementById(id);
const t = k => T[lang][k] || k;
function render() {
  document.documentElement.lang = lang;
  document.querySelectorAll("[data-t]").forEach(e => { if (e.id !== "cvname" || !S || !S.cv_name) e.textContent = t(e.dataset.t); });
  document.querySelectorAll(".lang button[data-l]").forEach(b => b.classList.toggle("on", b.dataset.l === lang));
  const v = $("english").value;
  $("english").innerHTML = Object.entries(t("levels")).map(([k, l]) => `<option value="${k}">${l}</option>`).join("");
  if (v) $("english").value = v;
  testHint();
}
function testHint() {
  const on = $("test_mode").checked, h = $("test_hint");
  h.textContent = t(on ? "test_on" : "test_off"); h.style.color = on ? "var(--warn)" : "var(--ok)"; h.style.fontWeight = 600;
}
$("test_mode").onchange = testHint;
async function api(path, body) {
  const r = await fetch(path, { method: body ? "POST" : "GET", headers: { "X-Token": TOKEN, "Content-Type": "application/json" }, body: body ? JSON.stringify(body) : undefined });
  return r.json();
}
function msg(id, text, cls) { const e = $(id); e.textContent = text; e.className = "msg " + (cls || ""); }
function err(id, r) { msg(id, t("err_" + r.error) + (r.detail ? " (" + r.detail + ")" : ""), "err"); }
function fill(p, show = true) {
  const reviewedPresets = p ? (S.settings.question_presets || []) : [];
  p = p || {};
  for (const k of ["first_name","last_name","email","phone_intl","city","country","linkedin","github","total_years","cover_letter"]) $(k).value = p[k] ?? "";
  const parts = (p.phone_intl || "").match(/^\s*\+([1-9]\d{0,2})[\s-]+([\d\s().-]+)\s*$/);
  $("phone_country_code").value = parts ? "+" + parts[1] : "";
  $("phone_national").value = parts ? parts[2].replace(/\D/g, "") : "";
  $("replace_prefilled_contacts").checked = !!p.phone_intl && S.settings.replace_prefilled_contacts === true;
  $("job_titles").value = (p.job_titles || []).join("\n");
  $("question_presets").replaceChildren();
  const templates = S.question_templates || [];
  for (let i = 0; i < 10; i++) {
    const row = document.createElement("div"), question = document.createElement("input"), answer = document.createElement("textarea");
    row.className = "question-preset";
    question.className = "preset-question"; question.value = reviewedPresets[i]?.question ?? templates[i] ?? "";
    question.setAttribute("aria-label", "Question " + (i + 1));
    answer.className = "preset-answer"; answer.value = reviewedPresets[i]?.answer ?? "";
    answer.setAttribute("aria-label", "Confirmed answer " + (i + 1));
    answer.style.minHeight = "50px";
    row.append(question, answer); $("question_presets").append(row);
  }
  if (show) { $("s2").classList.remove("hidden"); $("s3").classList.remove("hidden"); }
}
async function load() {
  S = await api("/api/state");
  if (S.cv_name) { $("cvname").textContent = S.cv_name; $("drop").classList.add("has"); }
  $("keybox").classList.toggle("hidden", S.has_key);
  $("changekey").classList.toggle("hidden", !S.has_key);
  const st = S.settings; render();
  $("english").value = st.english || "3"; const ms = st.monthly_salary_by_currency || {}, cur = st.home_currency || st.currency || "USD";
  $("currency").value = cur; $("salary").value = ms[cur] ?? (st.salary ? Math.round(st.salary / 12) : "");
  $("salary_eur").value = ms.EUR ?? ""; $("salary_usd").value = ms.USD ?? ""; $("notice").value = st.notice || "";
  $("locations").value = (st.locations || S.default_locations).join("\n");
  $("target_technologies").value = (st.target_technologies || []).join("\n");
  $("excluded_title_words").value = (st.excluded_title_words || []).join("\n");
  $("excluded_technologies").value = (st.excluded_technologies || []).join("\n");
  const on = st.sources || Object.keys(SOURCES);
  $("sources").innerHTML = Object.entries(SOURCES).map(([k, v]) => `<label><input type="checkbox" value="${k}" ${on.includes(k) ? "checked" : ""}> ${v}</label>`).join("");
  $("test_mode").checked = st.test_mode ?? false;
  fill(S.profile, !!S.profile);
  if (S.profile_error) err("m1", { error: "invalid_profile", detail: S.profile_error });
  if (S.llm_error) err("m1", { error: "invalid_llm_config", detail: S.llm_error });
  else if (S.llm && !S.llm.available) err("m1", { error: "llm_not_implemented" });
  if (S.running) watch();
  await refreshDeferred();
  render();
}
$("drop").onclick = () => $("file").click();
$("file").onchange = async () => {
  const f = $("file").files[0]; if (!f) return;
  const data = await new Promise(res => { const r = new FileReader(); r.onload = () => res(r.result.split(",")[1]); r.readAsDataURL(f); });
  const r = await api("/api/cv", { name: f.name, data });
  if (r.error) return err("m1", r);
  S.cv_name = r.cv_name; S.cv_hash = r.cv_hash;
  $("cvname").textContent = r.cv_name; $("drop").classList.add("has");
  if (r.profile_reset) { S.profile = null; S.settings.question_presets = []; S.settings.replace_prefilled_contacts = false; fill(null); msg("m1", t("cv_replaced")); }
  else msg("m1", "");
};
$("analyze").onclick = async () => {
  if (!S.cv_name) return err("m1", { error: "no_cv" });
  $("analyze").disabled = true; msg("m1", t("analyzing"));
  const r = await api("/api/analyze", { key: $("key").value });
  $("analyze").disabled = false;
  if (r.error) {
    if (r.error === "cv_changed") await load();
    if (r.error === "analyze_failed" || r.error === "cv_changed") { S.profile = null; fill(null); }
    err("m1", r); return;
  }
  S.profile = r.profile; S.cv_hash = r.cv_hash; S.cv_name = r.cv_name;
  $("cvname").textContent = r.cv_name;
  $("keybox").classList.add("hidden"); $("changekey").classList.remove("hidden"); $("key").value = ""; msg("m1", t("analyzed"), "ok"); fill(r.profile); $("s2").scrollIntoView({ behavior: "smooth" });
};
$("changekey").onclick = () => { $("keybox").classList.remove("hidden"); $("changekey").classList.add("hidden"); $("key").focus(); };
$("manual").onclick = () => { fill(S.profile); $("s2").scrollIntoView({ behavior: "smooth" }); };
const lines = id => $(id).value.split("\n").map(s => s.trim()).filter(Boolean);
function startBody() {
  const profile = {};
  for (const k of ["first_name","last_name","email","phone_intl","city","country","linkedin","github","total_years","cover_letter"]) profile[k] = $(k).value.trim();
  profile.job_titles = lines("job_titles");
  const body = { profile, cv_hash: S.cv_hash, locations: lines("locations"), salary: $("salary").value, currency: $("currency").value,
    question_presets: [...document.querySelectorAll(".question-preset")].map(row => ({ question: row.querySelector(".preset-question").value.trim(), answer: row.querySelector(".preset-answer").value.trim() })).filter(entry => entry.question || entry.answer),
    phone_country_code: $("phone_country_code").value.trim(), phone_national: $("phone_national").value.trim(),
    replace_prefilled_contacts: $("replace_prefilled_contacts").checked,
    target_technologies: lines("target_technologies"), excluded_technologies: lines("excluded_technologies"),
    excluded_title_words: lines("excluded_title_words"),
    salary_eur: $("salary_eur").value, salary_usd: $("salary_usd").value, notice: $("notice").value, english: $("english").value,
    sources: [...document.querySelectorAll("#sources input:checked")].map(e => e.value), test_mode: $("test_mode").checked };
  return body;
}
$("start").onclick = async () => {
  const body = startBody();
  document.querySelectorAll("input.missing").forEach(e => e.classList.remove("missing"));
  const r = await api("/api/start", body);
  if (r.error === "cv_changed") { await load(); S.profile = null; fill(null); }
  if (r.error === "missing") { r.fields.forEach(f => $(f).classList.add("missing")); $(r.fields[0]).scrollIntoView({ behavior: "smooth", block: "center" }); }
  if (r.error) return err("m3", r);
  logFrom = 0; $("log").textContent = ""; watch();
};
$("stop").onclick = () => api("/api/stop", {});
$("quit").onclick = async () => {
  await api("/api/quit", {});
  uiClosed = true; clearInterval(poll); poll = null;
  document.body.innerHTML = `<main><h1>Auto Apply</h1><p>${t("quit_done")}</p></main>`;
};
$("results").onclick = () => api("/api/open", { folder: "data" });
async function refreshDeferred() {
  let r;
  try { r = await api('/api/deferred'); }
  catch (error) { if (!uiClosed) err('review_message', {error: 'review_unavailable'}); return; }
  if (uiClosed) return;
  const root = $('deferred_jobs'); root.replaceChildren();
  for (const [category, label] of [['manual', 'deferred_manual'], ['limit', 'deferred_limits']]) {
    const jobs = (r.jobs || []).filter(job => (job.category || 'manual') === category);
    const heading = document.createElement('h3'); heading.textContent = t(label) + ' (' + jobs.length + ')'; root.append(heading);
    for (const job of jobs) {
    const row = document.createElement('div'), title = document.createElement('h3'), reason = document.createElement('p');
    title.textContent = job.title + ' — ' + job.company;
    reason.textContent = job.reason + ((job.related_job_ids || []).length > 1 ? ' — ' + job.related_job_ids.length + ' listings / 1 ATS application' : '');
    row.append(title, reason);
    const open = document.createElement('button'); open.className = 'secondary';
    open.textContent = t(job.can_assist ? (category === 'limit' ? 'review_retry' : 'review_open') : 'review_external');
    open.onclick = async () => {
      if (job.can_assist) {
        if (!$('test_mode').checked && !confirm(t('review_live_confirm'))) return;
        const result = await api('/api/review/start', {...startBody(), job_id: job.job_id});
        if (result.error) return err('review_message', result);
        logFrom = 0; $('log').textContent = ''; watch();
      } else {
        if (!confirm(t('review_external_confirm'))) return;
        const result = await api('/api/deferred/open', {job_id: job.job_id});
        if (result.error) err('review_message', result);
      }
    };
    const sent = document.createElement('button'); sent.className = 'secondary'; sent.textContent = t('review_mark_sent');
    sent.onclick = async () => {
      if (!confirm(t('review_sent_confirm'))) return;
      const result = await api('/api/deferred/mark-sent', {job_id: job.job_id});
      if (result.error) return err('review_message', result);
      await refreshDeferred();
    };
    row.append(open);
    if (job.can_assist) {
      const website = document.createElement('button'); website.className = 'secondary'; website.textContent = t('review_external');
      website.onclick = async () => {
        if (!confirm(t('review_external_confirm'))) return;
        const result = await api('/api/deferred/open', {job_id: job.job_id});
        if (result.error) err('review_message', result);
      };
      row.append(website);
    }
    row.append(sent); root.append(row);
    }
  }
}
function showReview(review) {
  activeReview = review;
  $('review_panel').classList.toggle('hidden', !review);
  if (!review) return;
  $('review_status').textContent = (review.test_mode ? 'TEST — ' : 'LIVE — ') + review.phase + (review.result ? ': ' + review.result : '');
  $('review_issues').textContent = review.issues.join('\n');
  $('review_continue').disabled = $('review_skip').disabled = review.phase !== 'waiting';
}
async function reviewCommand(action) {
  if (!activeReview) return;
  const result = await api('/api/review/action', {action, step: activeReview.step});
  if (result.error) err('review_message', result);
}
$('refresh_deferred').onclick = refreshDeferred;
$('review_continue').onclick = () => reviewCommand('continue');
$('review_skip').onclick = () => reviewCommand('skip');
function watch() {
  if (uiClosed) return;
  $("s4").classList.remove("hidden"); $("start").disabled = true; $("stop").classList.remove("hidden"); msg("m3", t("running"));
  clearInterval(poll);
  poll = setInterval(async () => {
    let r;
    try { r = await api("/api/log?from=" + logFrom); }
    catch (error) { if (uiClosed) return; throw error; }
    if (uiClosed) return; // An in-flight response may arrive after the UI has closed.
    showReview(r.review || null);
    if (r.lines.length) { logFrom += r.lines.length; const l = $("log"); l.textContent += r.lines.join("\n") + "\n"; l.scrollTop = l.scrollHeight; }
    if (!r.running) { clearInterval(poll); $("start").disabled = false; $("stop").classList.add("hidden"); msg("m3", t("finished"), "ok"); await refreshDeferred(); }
  }, 1500);
}
document.querySelectorAll(".lang button[data-l]").forEach(b => b.onclick = () => {
  if (!validLanguage(b.dataset.l)) return;
  lang = b.dataset.l;
  try { localStorage.setItem("lang", lang); } catch (_) {}
  render();
});
load();
</script></body></html>
"""


def selftest() -> None:
    """Verify that the packaged app can drive Chrome (used by release CI)."""
    os.environ["HEADLESS"] = "true"
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        ctx = apply.launch(pw)
        page = ctx.new_page()
        page.set_content("<title>selftest-ok</title>")
        title = page.title()
        # Check bundled HTTPS certificates using OpenRouter's public model catalog.
        models = cv_profile.request("/models", "")["data"]
        (apply.HOME / "selftest.txt").write_text(f"{title} models={len(models)}", encoding="utf-8")  # Windowed apps may have no console.
        ctx.close()


def main() -> None:
    if os.getenv("AUTOAPPLY_SELFTEST"):
        return selftest()
    apply.load_settings()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    url = f"http://127.0.0.1:{server.server_port}/"
    apply.log(f"Interface: {url}")
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
