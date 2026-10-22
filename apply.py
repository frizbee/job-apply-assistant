#!/usr/bin/env python3
"""Multi-source job application assistant, including LinkedIn Easy Apply.

Filters job postings using config.yaml and records application history in
data/applied.json to prevent blind retries. Browser sessions are stored in
.browser-profile; sign in manually in Chrome when required.

The terminal collects the CV path, salary expectations, English level and notice
period. Optional CV analysis uses free OpenRouter models; the user reviews the
extracted profile before it is used. Preferences live in data/user_settings.json.

Environment variables:
  DRY_RUN   "true" fills forms without intentionally sending applications
  HEADLESS  "true" runs without a visible browser window
"""
import csv
import datetime as dt
import hashlib
import html
import json
import os
import random
import re
import secrets
import sys
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from pathlib import Path
from urllib.parse import urlencode, urlparse

import llm_config
import llm_credentials
import job_metadata
import job_relevance
import job_rechecks
from playwright.sync_api import Locator, Page, sync_playwright
from playwright.sync_api import TimeoutError as PWTimeout

ROOT = Path(__file__).resolve().parent  # Source code and bundled default config.yaml.
# User data: project directory from source, ~/AutoApply in the packaged app.
HOME = Path(os.getenv("AUTOAPPLY_HOME") or (Path.home() / "AutoApply" if getattr(sys, "frozen", False) else ROOT))
(HOME / "data").mkdir(parents=True, exist_ok=True)
CFG = llm_config.load_yaml(((HOME if (HOME / "config.yaml").exists() else ROOT) / "config.yaml").read_text(encoding="utf-8"))
APPLIED_FILE = HOME / "data" / "applied.json"
LOG_FILE = HOME / "data" / "applications.csv"
LAST_RUN_FILE = HOME / "data" / "last_run.txt"
MANUAL_FILE = HOME / "data" / "manual_apply.csv"
SHOTS = HOME / "screenshots"
PROFILE_DIR = HOME / ".browser-profile"
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
DEADLINE = time.time() + CFG["limits"]["max_runtime_minutes"] * 60
STOP = threading.Event()  # Set by the GUI Stop button.
PROFILE = CFG["profile"]
EXTERNAL = CFG.get("external", {}).get("enabled", False)
SETTINGS_FILE = HOME / "data" / "user_settings.json"
SETTINGS: dict = {}

# English level -> candidate form answers, in preference order.
ENGLISH_LEVELS = {
    "1": ("Native / bilingual", ["Native or bilingual", "Native", "Bilingual", "Fluent", "C2"]),
    "2": ("Fluent / professional", ["Full professional", "Professional working", "Fluent", "Advanced", "C1"]),
    "3": ("Conversational / intermediate", ["Conversational", "Intermediate", "Limited working", "B2", "Komunikatywny", "Średnio zaawansowany"]),
    "4": ("Basic", ["Elementary", "Basic", "Beginner", "A2"]),
}

EASY_APPLY_RE = re.compile(r"easy apply|kolay başvuru", re.I)
PLACEHOLDER_RE = re.compile(r"select an option|seçenek belirleyin|seçin|seçiniz|wybierz|bitte wählen|choose an option", re.I)
YEARS_RE = re.compile(r"how many years|years of|yıl|ile lat|wie viele jahre|combien d.ann|cuántos años", re.I)
EXPERIENCE_RE = re.compile(r"experience|deneyim|tecrübe|erfahrung|expérience|experiencia|doświadczen", re.I)
SENSITIVE_RE = re.compile(
    r"sponsor|visa|authori[sz]|legally|citizen|right to work|work permit|convict|criminal|fraud|felon|"
    r"\bagree\b|consent|privacy|\bterms\b|acknowledge|certif|attest|\bconfirm\b|"
    r"bachelor|degree|education|student|enroll|relocat|commut|on-?site|hybrid|\boffice\b|"
    r"start date|when can you start|available to start", re.I)
NARRATIVE_RE = re.compile(
    r"^\s*(describe|explain|tell (us|me)|why\b|how (do|did|would|have|will) you|"
    r"share\b|elaborate|anlat|açıkla|neden)", re.I)

QUESTION_JS = r"""el => {
  const t = s => (s || '').replace(/\s+/g, ' ').trim();
  if (el.id) {
    const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
    if (l && t(l.innerText) && el.type !== 'radio') return t(l.innerText);
  }
  const lb = el.getAttribute('aria-labelledby');
  if (lb) {
    const txt = lb.split(' ').map(id => document.getElementById(id)).filter(Boolean).map(e => t(e.innerText)).join(' ');
    if (txt) return txt;
  }
  const fs = el.closest('fieldset');
  if (fs) { const lg = fs.querySelector('legend'); if (lg && t(lg.innerText)) return t(lg.innerText); }
  // New LinkedIn form: climb to the first meaningful line if the question is unbound.
  const noise = /^(\d+\s*\/\s*\d+.*|.*karakter|.*characters?|geçersiz giriş|invalid.*|.*zorunlu.*|.*required.*|yes|no|evet|hayır)$/i;
  const group = el.type === 'radio' && el.name
    ? [...document.querySelectorAll(`input[type=radio][name="${CSS.escape(el.name)}"]`)] : [el];
  const opts = new Set(group.map(r => t((r.id && document.querySelector(`label[for="${CSS.escape(r.id)}"]`) || r.parentElement || {}).innerText)));
  let p = el.parentElement;
  for (let i = 0; i < 8 && p; i++, p = p.parentElement) {
    if (!group.every(r => p.contains(r))) continue;
    const lines = (p.innerText || '').split('\n').map(t).filter(l => l && !noise.test(l) && !opts.has(l));
    if (lines.length) return lines[0].slice(0, 300);
  }
  return t(el.getAttribute('aria-label') || el.getAttribute('placeholder'));
}"""


# Only these explicit pre-submission statuses may be retried on a later run.
RETRYABLE_STATUSES = frozenset({"modal_not_opened", "blocked", "skipped_no_button", "skipped_no_form",
                               "skipped_unanswered", "skipped_too_many_steps", "skipped_external_limit", "pre_submission_error",
                               "skipped_run_limit", "skipped_source_limit", "evaluation_passed"})
LEGACY_UNCERTAIN_STATUSES = frozenset({"skipped_unconfirmed", "external_unconfirmed", "external_unanswered", "skipped_error"})
UNCERTAIN_STATUSES = frozenset({"submission_pending", "submission_unconfirmed"}) | LEGACY_UNCERTAIN_STATUSES


def can_retry(status: str) -> bool:
    """Unknown/new statuses are not retryable unless explicitly reviewed here."""
    return status in RETRYABLE_STATUSES or (status == "no_easy_apply" and EXTERNAL)


class LimitReached(Exception):
    """The application limit or runtime limit has been reached."""


class Stopped(BaseException):
    """User cancellation; BaseException keeps source-level Exception handlers from swallowing it."""


class BrowserClosed(Stopped):
    """The user closed the browser."""


def browser_closed(e: BaseException) -> bool:
    return bool(re.search(r"has been closed|Target closed|Browser closed|Connection closed", str(e)))


class SessionLost(Exception):
    """The LinkedIn session expired or its cookies are no longer valid."""


ATTEMPTS = {'total': 0, 'by_source': {}}
ATTEMPT_LOCK = threading.Lock()
CURRENT_SUBMISSION_SOURCE = ContextVar('submission_source', default=None)
SUBMISSION_SOURCES = frozenset({'linkedin', 'indeed', 'wellfound', 'remoteok', 'himalayas', 'weworkremotely'})


def source_for_job(job_id: str) -> str:
    """Company-site submissions belong to their originating board, not the ATS."""
    source = CURRENT_SUBMISSION_SOURCE.get() or str(job_id).partition(':')[0]
    source = 'weworkremotely' if source == 'wwr' else source
    return source if source in SUBMISSION_SOURCES else 'linkedin'


@contextmanager
def submission_source(source: str):
    token = CURRENT_SUBMISSION_SOURCE.set(source)
    try:
        yield
    finally:
        CURRENT_SUBMISSION_SOURCE.reset(token)


def reset_submission_attempts() -> None:
    with ATTEMPT_LOCK:
        ATTEMPTS.update(total=0, by_source={})


def _submission_quota_status(source: str | None) -> str | None:
    limits = CFG['limits']
    if ATTEMPTS['total'] >= limits['max_applications_per_run']:
        return 'skipped_run_limit'
    if source is not None and ATTEMPTS['by_source'].get(source, 0) >= limits.get('per_source', 15):
        return 'skipped_source_limit'
    return None


def submission_quota_status(source: str | None = None) -> str | None:
    with ATTEMPT_LOCK:
        return _submission_quota_status(source)


def reserve_submission_attempt(source: str) -> str | None:
    """Atomically charge a slot before pending persistence or the final click."""
    with ATTEMPT_LOCK:
        denied = _submission_quota_status(source)
        if denied:
            return denied
        ATTEMPTS['total'] += 1
        ATTEMPTS['by_source'][source] = ATTEMPTS['by_source'].get(source, 0) + 1
    return None


def release_submission_attempt(source: str) -> None:
    """Refund only when the click was definitely never invoked."""
    with ATTEMPT_LOCK:
        ATTEMPTS['total'] -= 1
        ATTEMPTS['by_source'][source] -= 1


def preview_application(job_id: str) -> str:
    if STOP.is_set():
        raise Stopped
    return reserve_submission_attempt(source_for_job(job_id)) or 'dry_run'


LOG_LINES: list[str] = []  # Read by the GUI log endpoint.


def log(msg: str) -> None:
    line = f"[{dt.datetime.now():%H:%M:%S}] {msg}"
    LOG_LINES.append(line)
    try:
        with (HOME / "data" / "run.log").open("a", encoding="utf-8") as f:
            f.write(f"{dt.date.today()} {line}\n")
    except OSError:
        pass
    try:
        print(line, flush=True)
    except Exception:
        pass  # Windowed builds may not have a console.


def pause(a: float = 1.5, b: float = 3.5) -> None:
    if STOP.wait(random.uniform(a, b)):
        raise Stopped


# ---------------------------------------------------------------- state

def load_applied() -> dict:
    try:
        applied = json.loads(APPLIED_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise RuntimeError("Application history is corrupt; restore it before running to avoid duplicate submissions") from e
    if not isinstance(applied, dict) or any(not isinstance(v, dict) or not isinstance(v.get("status"), str)
                                          for v in applied.values()):
        raise RuntimeError("Invalid application history; refusing to submit without reliable history")
    migrated = {job_id: v for job_id, v in applied.items() if v["status"] in LEGACY_UNCERTAIN_STATUSES}
    if migrated:
        details = {}
        if LOG_FILE.exists():
            with LOG_FILE.open(newline="", encoding="utf-8") as f:
                details = {row.get("job_id"): row for row in csv.DictReader(f)}
        for job_id, value in migrated.items():
            value.update(legacy_status=value["status"], status="submission_unconfirmed")
            row = details.get(job_id, {})
            for field in ("title", "company", "url"):
                value.setdefault(field, row.get(field, ""))
            if not value.get("key") and value.get("title") and value.get("company"):
                value["key"] = job_key(value["title"], value["company"])
        write_applied(applied)
        for job_id, value in migrated.items():
            record_manual(job_id, value.get("title", ""), value.get("company", ""), value.get("url", ""),
                          "migrated " + value["legacy_status"] + ": check application history before any manual retry")
        log(f"Migrated {len(migrated)} uncertain attempts to manual review; automatic retries disabled.")
    return applied


def write_applied(applied: dict) -> None:
    temporary = APPLIED_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(applied, indent=1, ensure_ascii=False), encoding="utf-8")
    temporary.replace(APPLIED_FILE)


def job_key(title: str, company: str) -> str:
    """Company/title key for recognizing the same job across sources."""
    return re.sub(r"\W+", "", f"{company}|{title}".lower())


def applied_elsewhere(applied: dict, title: str, company: str) -> bool:
    key = job_key(title, company)
    return any(v.get("key") == key and (v["status"].startswith("applied")
               or v["status"] in UNCERTAIN_STATUSES) for v in applied.values())


def record(applied: dict, job_id: str, title: str, company: str, status: str, url: str = "") -> None:
    previous = applied.get(job_id, {})
    decisions = list(previous.get('decisions', []))
    if previous and not decisions:
        decisions.append(job_rechecks.snapshot(previous))
    applied[job_id] = {**previous, "status": status, "date": dt.date.today().isoformat(), "key": job_key(title, company),
                       "title": title, "company": company, "url": url or f"https://www.linkedin.com/jobs/view/{job_id}/",
                       'evaluated_at': dt.datetime.now(dt.timezone.utc).isoformat(), 'reason': status,
                       'evaluation_revision': job_rechecks.revision(CFG, SETTINGS)}
    applied[job_id].pop('metadata_diagnostic', None)
    applied[job_id].pop('technology_diagnostic', None)
    decisions.append(job_rechecks.snapshot(applied[job_id]))
    applied[job_id]['decisions'] = decisions
    write_applied(applied)
    new_file = not LOG_FILE.exists()
    with LOG_FILE.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["date", "job_id", "title", "company", "status", "url"])
        w.writerow([dt.datetime.now().isoformat(timespec="minutes"), job_id, title, company,
                    status, url or f"https://www.linkedin.com/jobs/view/{job_id}/"])


BLOCKED_RE = re.compile(r"just a moment|attention required|additional verification|security check|"
                        r"humans only|verify you are human|are you a robot|captcha", re.I)


def blocked(page: Page) -> bool:
    try:
        return bool(BLOCKED_RE.search(page.title()) or BLOCKED_RE.search(page.locator("body").inner_text()[:1500]))
    except Exception:
        return False


def wait_human(page: Page, check, what: str, minutes: int = 5) -> bool:
    """Wait for the user to complete verification/login until check(page) succeeds."""
    if check(page):
        return True
    if os.getenv("HEADLESS", "false").lower() == "true":
        return False
    log(f"  !! Please {what} in the open Chrome window (waiting up to {minutes} minutes)...")
    end = time.time() + minutes * 60
    while time.time() < end:
        pause(3, 3)
        if check(page):
            pause(2, 3)
            return True
    log(f"  !! Required action not completed: {what}; skipping this source.")
    return False


def screenshot(page: Page, name: str) -> None:
    SHOTS.mkdir(exist_ok=True)
    try:
        page.screenshot(path=str(SHOTS / f"{name}.png"), full_page="_ext_" in name)
    except Exception:
        pass


# ---------------------------------------------------------------- answers

def answer(question: str, numeric: bool = False) -> list[str]:
    """Candidate answers to a question; the first matching rule wins."""
    q = question.lower()
    result: list[str] = []
    # Exact, user-confirmed answers take precedence over generic regex rules.
    confirmed = CFG.get("confirmed_answers", {})
    exact = next((v for k, v in confirmed.items() if k.strip().casefold() == question.strip().casefold()), None)
    if exact is not None:
        values = exact if isinstance(exact, list) else [exact]
        result = [str(v) for v in values if v is not None and str(v).strip()]
        return [v for v in result if re.fullmatch(r"\d+(?:\.\d+)?", v)] if numeric else result
    if SENSITIVE_RE.search(q) or NARRATIVE_RE.search(q):
        return []
    for rule in CFG["answers"]:
        if re.search(rule["match"], q):
            ans = rule["answer"]
            result = [str(a) for a in (ans if isinstance(ans, list) else [ans])]
            break
    if not result and (YEARS_RE.search(q) or EXPERIENCE_RE.search(q)):
        for pattern, years in CFG["skill_years"].items():
            if re.search(rf"(?<!\w)(?:{pattern})(?!\w)", q):
                result = [str(years)]
                break
    if "__SALARY__" in result:
        result = [salary_answer(q)]
    if "__ENGLISH__" in result:
        result = ENGLISH_LEVELS.get(SETTINGS.get("english"), ("", []))[1]
    result = [v for v in map(placeholder, result) if v]
    if numeric:
        result = [a for a in result if re.fullmatch(r"\d+(?:\.\d+)?", a)]
    return result


CURRENCY_RE = {
    "USD": r"\busd\b|\$|us dollars?|\bdollars?\b",
    "EUR": r"\beur\b|€|\beuros?\b",
    "GBP": r"\bgbp\b|£|\bpounds?\b",
    "TRY": r"\btry\b|\btl\b|₺|lira",
    "PLN": r"\bpln\b|zł|zloty",
    "CHF": r"\bchf\b|swiss franc",
    "INR": r"\binr\b|₹|rupee",
    "CAD": r"\bcad\b|canadian dollar",
}
RATES: dict[str, float] = {}


def rates() -> dict[str, float]:
    """Units per USD from free sources; an empty result prevents conversion."""
    if not RATES:
        import urllib.request
        from cv_profile import SSL_CTX
        for url in ("https://api.frankfurter.dev/v1/latest?base=USD", "https://open.er-api.com/v6/latest/USD"):
            try:
                with urllib.request.urlopen(url, timeout=15, context=SSL_CTX) as r:
                    payload = json.loads(r.read())
                if payload.get('base', payload.get('base_code')) != 'USD' or not isinstance(payload.get('rates'), dict):
                    continue
                valid = {code: value for code, value in payload['rates'].items() if positive_decimal(value) is not None}
                if positive_decimal(valid.get('USD', 1)) != Decimal(1):
                    continue
                RATES.update(valid, USD=1.0)
                if "TRY" in RATES:
                    break
            except Exception:
                continue
    return RATES


def salary_answer(question: str) -> str:
    """Convert only explicit, unambiguous units; unknown amounts/FX require review."""
    q = question.lower()
    # Strip recognized names first so CAD dollars do not also become USD.
    remaining, currencies = q, set()
    patterns = {**CURRENCY_RE, 'USD': r'\busd\b|\bus dollars?\b',
                'CAD': r'\bcad\b|\bcanadian dollars?\b'}
    for code, pattern in patterns.items():
        if re.search(pattern, remaining):
            currencies.add(code)
            remaining = re.sub(pattern, ' ', remaining)
    # A bare $/dollar is not USD: many currencies share it. Unknown currency
    # names/codes must not be hidden by a recognized code elsewhere in the label.
    if re.search(r'\b(?:aud|nzd|sgd|hkd|jpy|cny|rmb|rub|brl|mxn|zar|sek|nok|dkk|aed|sar|krw|ils|pkr|bdt|ron|czk|huf|uah|idr|myr|thb|php|vnd)\b|[¥₩₽]|yen|yuan|rou?bles?|pesos?|australian|singapore|hong kong|new zealand', remaining):
        return ''
    if any(code not in {'per', 'net', 'the'} for code in re.findall(r'\b(?:in|currency|or)\s+([a-z]{3})\b', remaining)):
        return ''
    if len(currencies) != 1 or (re.search(r'\bdollars?\b|\$', remaining) and not currencies <= {'USD', 'CAD'}):
        return ''
    want = currencies.pop()
    periods = {name for name, pattern in {
        'annual': r'\bannual(?:ly)?\b|\byearly\b|(?:\bper\s+|/\s*)(?:year|annum)\b|yıllık',
        'monthly': r'\bmonthly\b|(?:\bper\s+|/\s*)month\b|aylık|monat|mensuel',
        'hourly': r'\bhourly\b|(?:\bper\s+|/\s*)(?:hour|hr)\b|saatlik|stündlich',
        'daily': r'\bdaily\b|(?:\bper\s+|/\s*)day\b|günlük',
    }.items() if re.search(pattern, q)}
    if len(periods) != 1 or re.search(r'\bweek|\bbiweekly\b|\bfortnight|\bquarter|\bsemi[ -]?|\bbiannual|\bbimonth|\bper (?:minute|second)|haftalık', q):
        return ''
    fixed = SETTINGS.get('monthly_salary_by_currency') or {}
    if not isinstance(fixed, dict):
        return ''
    amount = positive_decimal(fixed[want] if want in fixed else SETTINGS.get('salary'))
    if amount is None:
        return ''
    try:
        with localcontext() as context:
            context.prec = 40
            if want in fixed:
                amount *= 12
            else:
                have = SETTINGS.get('currency')
                if have not in CURRENCY_RE:
                    return ''
                if have != want:
                    fx = rates()
                    if not isinstance(fx, dict):
                        return ''
                    source, target = positive_decimal(fx.get(have)), positive_decimal(fx.get(want))
                    if source is None or target is None:
                        return ''
                    amount = amount / source * target
            amount /= {'annual': 1, 'monthly': 12, 'hourly': 2080, 'daily': 220}[periods.pop()]
            if re.search(r'in thousands|\(k\)|thousands of|bin (tl|usd|eur)', q):
                amount /= 1000
            rounded = amount.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            return decimal_text(rounded) if rounded > 0 else ''
    except (InvalidOperation, OverflowError, ValueError, TypeError, ArithmeticError):
        return ''


def positive_decimal(value) -> Decimal | None:
    """Reject booleans, nonfinite numbers and malformed/ambiguous numeric text."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return None
    text = str(value).strip()
    if not re.fullmatch(r'\d+(?:\.\d+)?', text) or len(text) > 24:
        return None
    number = Decimal(text)
    return number if number.is_finite() and number > 0 else None


def decimal_text(value: Decimal) -> str:
    text = format(value, 'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


def placeholder(a: str) -> str:
    """Resolve placeholders such as __SALARY__ from settings or the profile."""
    m = re.fullmatch(r"__([A-Z_]+)__", a)
    if not m:
        return a
    key = m.group(1).lower()
    value = SETTINGS.get(key)
    if value is None or value == "":
        value = PROFILE.get(key, "")
    return str(value) if value is not None else ""


def year_range(option: str) -> tuple[float, float] | None:
    """'Less than 1 year' -> (0, 1), '1-3 years' -> (1, 3), '5+ years' -> (5, inf), 'None' -> (0, 0)."""
    o = option.lower()
    if re.fullmatch(r"\s*(none|no experience|0|hiç|yok)\s*", o):
        return (0, 0)
    nums = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", o)]
    if not nums:
        return None
    if re.search(r"less than|under|fewer than|<|az|below|up to", o):
        return (0, nums[0])
    if re.search(r"\+|more than|over|above|or more|at least|>|üzeri|fazla", o):
        return (nums[0], float("inf"))
    if len(nums) >= 2:
        return (nums[0], nums[1])
    return (nums[0], nums[0])


def choose_years(years: float, opts: list[tuple[int, str]]) -> int | None:
    """Choose only a range containing the confirmed years; never guess the nearest."""
    ranges = [(i, year_range(o)) for i, o in opts]
    ranges = [(i, r) for i, r in ranges if r]
    if len(ranges) < max(2, len(opts) // 2):
        return None
    inside = []
    for i, (lo, hi) in ranges:
        text = dict(opts)[i].lower()
        lower = years > lo if re.search(r"more than|over|above|>|fazla", text) else years >= lo
        upper = years < hi if re.search(r"less than|under|fewer than|<|az|below", text) else years <= hi
        if lower and upper:
            inside.append(i)
    if len(inside) == 1:
        return inside[0]
    return None


def match_option(candidates: list[str], options: list[str]) -> int | None:
    """Only exact, unambiguous matches; 'No' must not match 'No experience'."""
    def normalize(value: str) -> str:
        return " ".join(value.split()).casefold()
    for candidate in candidates:
        matches = [i for i, option in enumerate(options)
                   if option.strip() and not PLACEHOLDER_RE.search(option)
                   and normalize(option) == normalize(candidate)]
        if len(matches) == 1:
            return matches[0]
    return None


def choose_option(question: str, options: list[str]) -> int | None:
    """Return the index of an unambiguous matching answer."""
    opts = [(i, o.strip()) for i, o in enumerate(options) if o.strip() and not PLACEHOLDER_RE.search(o)]
    if not opts:
        return None
    if YEARS_RE.search(question) or EXPERIENCE_RE.search(question):
        years = answer(question, numeric=True)
        idx = choose_years(float(years[0]), opts) if years else None
        if idx is not None:
            return idx
    return match_option(answer(question), options)


def question_of(el: Locator) -> str:
    try:
        return el.evaluate(QUESTION_JS) or ""
    except Exception:
        return ""


# ---------------------------------------------------------------- form filling

class UnsafeFormControl(ValueError):
    """Only the explicit final-submit path may activate a native submit control."""


UNSAFE_SUBMIT_SELECTOR = "button:not([type='button' i]), button[formaction], input[type='submit' i], input[type='image' i]"
NON_SUBMITTING_CONTROL_JS = r"""el => {
    const submits = e => e && ((e.tagName === 'BUTTON' && (e.type !== 'button' || e.hasAttribute('formaction')))
        || (e.tagName === 'INPUT' && /^(submit|image)$/i.test(e.type)));
    if (submits(el.closest('button, input'))) return false;
    if (submits(el.parentElement?.closest('button, input'))) return false;
    const label = el.closest('label');
    if (label && submits(label.control)) return false;
    return !el.querySelector("button:not([type='button' i]), button[formaction], input[type='submit' i], input[type='image' i]");
}"""


def click_form_control(control: Locator, timeout: int = 5000) -> None:
    """Reject native submit actions during filling, in both test and live modes."""
    if control.evaluate(NON_SUBMITTING_CONTROL_JS) is not True:
        raise UnsafeFormControl("submit-capable control requires manual review")
    # Keep the native-type restriction in the locator too: a later type change
    # must not make a previously safe element silently become a submit button.
    safe_selector = (f":not(:is({UNSAFE_SUBMIT_SELECTOR})):not(:is({UNSAFE_SUBMIT_SELECTOR}) *)"
                     f":not(:has(:is({UNSAFE_SUBMIT_SELECTOR})))")
    control.and_(control.page.locator(safe_selector)).click(timeout=timeout)


NAVIGATION_BUTTON_RE = re.compile(
    r"^\s*(?:next(?:\s+step)?|continue(?:\s+to\s+(?:the\s+)?(?:next\s+step|review(?:\s+your\s+application)?))?|"
    r"review(?:\s+(?:your\s+)?application)?|devam(?:\s+et)?|[iİıI]leri|(?:başvuruyu\s+)?(?:[iİıI]ncele|gözden\s+geç[iİıI]r))\s*$", re.I)
FINAL_BUTTON_RE = re.compile(
    r"^\s*(?:submit(?:\s+(?:your\s+)?application)?|send\s+(?:your\s+)?application|"
    r"(?:başvur(?:u|unu|uyu|unuzu)\s+)?gönder)\s*$", re.I)


def application_button_action(button: Locator) -> str:
    """Unknown or conflicting text/ARIA labels must never default to Next."""
    labels = [label.strip() for label in (button.inner_text(), button.get_attribute("aria-label") or "") if label.strip()]
    actions = {"submit" if FINAL_BUTTON_RE.fullmatch(label) else "next" if NAVIGATION_BUTTON_RE.fullmatch(label)
               else "unknown" for label in labels}
    return actions.pop() if len(actions) == 1 else "unknown"


def click_navigation(button: Locator) -> None:
    if application_button_action(button) != "next":
        raise UnsafeFormControl("unrecognized navigation action")
    navigation = button.and_(button.page.get_by_role("button", name=NAVIGATION_BUTTON_RE))
    if button.inner_text().strip():
        navigation = navigation.filter(has_text=NAVIGATION_BUTTON_RE)
    click_form_control(navigation)


RADIO_GROUP_JS = r"""fs => {
  const t = s => (s || '').replace(/\s+/g, ' ').trim();
  const lines = el => (el.innerText || '').split('\n').map(t).filter(Boolean);
  const roles = [...fs.querySelectorAll('[role=radio]')];
  const labels = roles.length
    ? roles.map(r => lines(r).pop() || '')
    : [...fs.querySelectorAll('input[type=radio]')].map(r => {
        const l = r.id && document.querySelector(`label[for="${CSS.escape(r.id)}"]`);
        if (l && t(l.innerText)) return t(l.innerText);
        // If the label is empty, use the nearest ancestor with option text.
        let p = r.parentElement;
        while (p && p !== fs && !t(p.innerText)) p = p.parentElement;
        return p && p !== fs ? t(p.innerText) : '';
      });
  let q = '';
  const lg = fs.querySelector('legend');
  if (lg) q = t(lg.innerText);
  if (!q && fs.parentElement) {
    const own = new Set(lines(fs));
    q = lines(fs.parentElement).find(l => !own.has(l)) || '';
  }
  if (!q && roles.length) q = t(roles[0].getAttribute('aria-label'));
  return [q, labels];
}"""


RADIO_GROUPS = "fieldset:has(input[type='radio']):visible, [role='radiogroup']:visible"
RADIO_SELECTION_JS = r"""root => {
    const native = [...root.querySelectorAll('input[type=radio]')];
    const roles = [...root.querySelectorAll('[role=radio]')];
    const nativeState = input => {
        const aria = input.getAttribute('aria-checked');
        return aria === null || aria === String(input.checked) ? input.checked : null;
    };
    if (!roles.length) return native.map(nativeState);
    // Do not overlook an actual submitted radio value outside the custom options.
    if (native.some(input => !roles.some(option => option === input || option.contains(input)))) return [];
    return roles.map(option => {
        if (option.matches('input[type=radio]')) return nativeState(option);
        const aria = option.getAttribute('aria-checked');
        if (aria !== 'true' && aria !== 'false') return null;
        const selected = aria === 'true';
        const backing = [...option.querySelectorAll('input[type=radio]')];
        if (backing.length > 1 || (backing.length === 1 && nativeState(backing[0]) !== selected)) return null;
        return selected;
    });
}"""


def radio_choice_confirmed(states, index) -> bool:
    """Unknown/inconsistent state is not False: require one exact, exclusive choice."""
    return (isinstance(states, list) and type(index) is int and 0 <= index < len(states)
            and states[index] is True and all(state is False for i, state in enumerate(states) if i != index))


def fill_radios(scope: Locator) -> list[str]:
    """Fill radio questions and return unresolved ones. New LinkedIn forms use
    div[role=radio] with empty labels; older forms use legend and label[for]."""
    unanswered: list[str] = []
    # Preselected options also need a confirmed matching answer.
    for fs in scope.locator(RADIO_GROUPS).all():
        q = ""
        try:
            q, labels = fs.evaluate(RADIO_GROUP_JS)
            idx = choose_option(q, labels) if q.strip() else None
            if idx is None:
                log(f"    ? unanswered question: {q[:90]}")
                unanswered.append(q or "radio group")
                continue
            before = fs.evaluate(RADIO_SELECTION_JS)
            if len(before) != len(labels):
                unanswered.append(q)
                continue
            if not radio_choice_confirmed(before, idx):
                items = fs.locator("[role='radio']")
                if items.count():
                    target = items.nth(idx)
                    if not target.is_enabled():
                        unanswered.append(q)
                        continue
                    click_form_control(target)
                else:
                    radio = fs.locator("input[type='radio']").nth(idx)
                    if radio.is_disabled():
                        unanswered.append(q)
                        continue
                    # Use the native control first, not an unbound text wrapper.
                    try:
                        radio.check(timeout=2000)
                    except PWTimeout:
                        # Hidden native inputs may be driven by a visible label/row.
                        if not radio.is_checked():
                            row = radio.locator("xpath=ancestor::*[normalize-space(.)!=''][1]")
                            click_form_control(row)
                            if not radio.is_checked():
                                radio.check(force=True, timeout=2000)
            pause(0.3, 0.7)
            current_q, current_labels = fs.evaluate(RADIO_GROUP_JS)
            after = fs.evaluate(RADIO_SELECTION_JS)
            if current_q != q or current_labels != labels or len(after) != len(labels) or not radio_choice_confirmed(after, idx):
                log(f"    ? radio selection could not be verified: {q[:90]}")
                unanswered.append(q)
        except Exception as e:
            if browser_closed(e):
                raise BrowserClosed() from None
            log(f"    ! could not fill radio group: {e.__class__.__name__}")
            unanswered.append(q or question_of(fs) or "radio group")
    return unanswered


# Standard external fields: (question/name/id pattern, profile key); first match wins.
STANDARD_FIELDS = [
    (r"first.?name|given.?name|vorname|\bad[ıi]?\b(?!.*soyad)", "first_name"),
    (r"last.?name|surname|family.?name|soyad|nachname", "last_name"),
    (r"full.?name|^name\b|your name|ad soyad", "full_name"),
    (r"e-?mail", "email"),
    (r"phone|mobile|telefon|\btel\b", "phone_intl"),
    (r"linkedin", "linkedin"),
    (r"github", "github"),
    (r"website|portfolio|personal (site|url)|other url", "github"),
    (r"current (company|employer)|^org\b| org$", "current_company"),
    (r"current (title|position|role)", "current_title"),
    (r"\blocation|\bcity\b|şehir|where are you (based|located)", "city"),
]


def fill_combobox(page: Page, el: Locator, q: str) -> bool:
    """Fill searchable Greenhouse/Ashby dropdowns (role=combobox)."""
    std = standard_answer(el, q)
    cands = [std] if std else answer(q)
    if not cands:
        return False
    try:
        click_form_control(el)
        pause(0.5, 1)
        if std:
            el.fill(std.split(",")[0])
            pause(1.5, 2.5)
        opts = page.locator("[role='option']:visible")
        texts = opts.all_inner_texts()
        if not texts:
            el.press("Escape")
            return False
        idx = choose_option(q, texts) if not std else match_option(cands, texts)
        if idx is None:
            el.press("Escape")
            return False
        click_form_control(opts.nth(idx))
        pause(0.3, 0.7)
        return True
    except Exception as e:
        if browser_closed(e):
            raise BrowserClosed() from None
        log(f"    ! could not fill dropdown: {e.__class__.__name__}")
        return False


YESNO_JS = r"""root => {
  // Mark Ashby Yes/No button pairs and return [question, any semantic selection].
  const t = s => (s || '').replace(/\s+/g, ' ').trim();
  const out = [];
  const yes = [...root.querySelectorAll('button')].filter(b => /^(yes|evet)$/i.test(t(b.innerText)));
  for (const b of yes) {
    const box = b.parentElement;
    const btns = [...box.querySelectorAll('button')];
    if (btns.length !== 2 || !/^(no|hayır)$/i.test(t(btns[1].innerText))) continue;
    let p = box.parentElement, q = '';
    for (let i = 0; i < 5 && p && !q; i++, p = p.parentElement)
      q = (p.innerText || '').split('\n').map(t).find(l => l && !/^(yes|no|evet|hayır)$/i.test(l)) || '';
    const picked = btns.some(x => ['aria-pressed', 'aria-checked', 'aria-selected'].some(a => x.getAttribute(a) === 'true'));
    box.setAttribute('data-yesno', out.length);
    out.push([q, picked]);
  }
  return out;
}"""

YESNO_SELECTION_JS = r"""box => {
    const t = s => (s || '').replace(/\s+/g, ' ').trim();
    const buttons = [...box.querySelectorAll('button')];
    if (buttons.length !== 2 || !/^(yes|evet)$/i.test(t(buttons[0].innerText))
        || !/^(no|hayır)$/i.test(t(buttons[1].innerText))) return {question: '', states: []};
    let p = box.parentElement, question = '';
    for (let i = 0; i < 5 && p && !question; i++, p = p.parentElement)
        question = (p.innerText || '').split('\n').map(t).find(l => l && !/^(yes|no|evet|hayır)$/i.test(l)) || '';
    const states = buttons.map(button => {
        const signals = ['aria-pressed', 'aria-checked', 'aria-selected'].map(a => button.getAttribute(a)).filter(v => v !== null);
        if (!signals.length || signals.some(v => v !== 'true' && v !== 'false') || signals.some(v => v !== signals[0])) return null;
        const selected = signals[0] === 'true';
        const backing = [...button.querySelectorAll('input[type=radio], input[type=checkbox]')];
        if (backing.length > 1 || (backing.length === 1 && backing[0].checked !== selected)) return null;
        return selected;
    });
    return {question, states};
}"""

CHECKGROUP_STATE_JS = r"""g => {
  const t = s => (s || '').replace(/\s+/g, ' ').trim();
  const label = c => {
    const l = c.id && document.querySelector(`label[for="${CSS.escape(c.id)}"]`);
    if (l && t(l.innerText)) return t(l.innerText);
    let p = c.parentElement;
    while (p && !t(p.innerText)) p = p.parentElement;
    return p ? t(p.innerText) : '';
  };
  const cbs = [...g.querySelectorAll('input[type=checkbox]')];
  const opts = cbs.map(label);
  let p = g, q = '';
  for (let i = 0; i < 4 && p && !q; i++, p = p.parentElement)
    q = (p.innerText || '').split('\n').map(t).find(l => l && !opts.includes(l)) || '';
  return {question: q, options: opts,
    states: cbs.map(c => {
      const aria = c.getAttribute('aria-checked');
      return c.indeterminate || (aria !== null && aria !== String(c.checked)) ? null : c.checked;
    }),
    disabled: cbs.map(c => c.matches(':disabled') || c.getAttribute('aria-disabled') === 'true')};
}"""

CHECKGROUP_JS = r"""root => {
  // Refresh markers: removed/restructured groups must not leave stale exemptions.
  for (const g of root.querySelectorAll('[data-checkgroup]')) g.removeAttribute('data-checkgroup');
  const groups = new Set();
  for (const c of root.querySelectorAll('input[type=checkbox]')) {
    let g = c.parentElement;
    while (g && g !== root && g.querySelectorAll('input[type=checkbox]').length < 2) g = g.parentElement;
    if (!g || g === root) continue;
    groups.add(g);
  }
  const out = [];
  const inspect = (""" + CHECKGROUP_STATE_JS + r""");
  for (const g of groups) {
    const state = inspect(g);
    g.setAttribute('data-checkgroup', out.length);
    out.push([state.question, state.options, state.states.some(s => s === true)]);
  }
  return out;
}"""


def fill_yesno(scope: Locator) -> list[str]:
    unanswered = []
    for i, (q, _picked) in enumerate(scope.evaluate(YESNO_JS)):
        idx = choose_option(q, ["Yes", "No"]) if q.strip() else None
        if idx is None:
            unanswered.append(q or "Yes/No question")
            continue
        try:
            group = scope.locator(f"[data-yesno='{i}']")
            before = group.evaluate(YESNO_SELECTION_JS)
            if before.get("question") != q or len(before.get("states", [])) != 2:
                unanswered.append(q)
                continue
            if not radio_choice_confirmed(before["states"], idx):
                click_form_control(group.locator('button').nth(idx))
            pause(0.3, 0.6)
            after = group.evaluate(YESNO_SELECTION_JS)
            if after.get("question") != q or len(after.get("states", [])) != 2 or not radio_choice_confirmed(after["states"], idx):
                unanswered.append(q)
        except Exception as e:
            if browser_closed(e):
                raise BrowserClosed() from None
            unanswered.append(q)
    return unanswered


def unconfirmed_radio_questions(scope: Locator) -> list[str]:
    """Recheck choices after other fields/uploads, without changing any answer."""
    issues = []
    for group in scope.locator(RADIO_GROUPS).all():
        q, labels = group.evaluate(RADIO_GROUP_JS)
        idx = choose_option(q, labels) if q.strip() else None
        states = group.evaluate(RADIO_SELECTION_JS)
        if len(states) != len(labels) or not radio_choice_confirmed(states, idx):
            issues.append(q or "radio group")
    for i, (q, _picked) in enumerate(scope.evaluate(YESNO_JS)):
        idx = choose_option(q, ['Yes', 'No']) if q.strip() else None
        state = scope.locator(f"[data-yesno='{i}']").evaluate(YESNO_SELECTION_JS)
        if state.get('question') != q or len(state.get('states', [])) != 2 or not radio_choice_confirmed(state['states'], idx):
            issues.append(q or "Yes/No question")
    # A preselected radio outside a recognized question group is not a known answer.
    issues += scope.locator("input[type='radio'], [role='radio']").evaluate_all(r"""els => els
        .filter(e => !e.closest("fieldset:has(input[type='radio']), [role='radiogroup']"))
        .map(e => 'unrecognized radio group: ' + (e.name || e.id || 'unknown question'))""")
    return issues


def checkbox_choices(question: str, options: list[str]) -> list[int] | None:
    """A multi-answer is a complete set, never a best-effort subset."""
    candidates = answer(question) if question.strip() else []
    if not candidates:
        return None
    picks = [match_option([candidate], options) for candidate in candidates]
    if any(index is None for index in picks) or len(set(picks)) != len(picks):
        return None
    return picks


def checkbox_group_confirmed(state, picks) -> bool:
    """Require the exact exclusive set and consistent native/ARIA states."""
    if not isinstance(state, dict) or not isinstance(picks, list) or not picks:
        return False
    options, states, disabled = (state.get(key) for key in ('options', 'states', 'disabled'))
    if not all(isinstance(values, list) for values in (options, states, disabled)):
        return False
    if len(options) < 2 or len(states) != len(options) or len(disabled) != len(options):
        return False
    if any(type(index) is not int or not 0 <= index < len(options) for index in picks) or len(set(picks)) != len(picks):
        return False
    return all(type(off) is bool and selected is (index in picks) and (index not in picks or not off)
               for index, (selected, off) in enumerate(zip(states, disabled)))


def unconfirmed_checkbox_questions(scope: Locator) -> list[str]:
    issues = []
    for i, (q, options, _picked) in enumerate(scope.evaluate(CHECKGROUP_JS)):
        picks = checkbox_choices(q, options)
        state = scope.locator(f"[data-checkgroup='{i}']").evaluate(CHECKGROUP_STATE_JS)
        if (state.get('question') != q or state.get('options') != options
                or not checkbox_group_confirmed(state, picks)):
            issues.append(q or 'checkbox group')
    # A group can shrink to one input after filling. Do not let that remaining
    # value bypass verification merely because it is no longer a multi-select.
    for cb in scope.locator("input[type='checkbox']").all():
        if cb.get_attribute('id') == 'follow-company-checkbox' or cb.is_disabled():
            continue
        if cb.evaluate("e => !!e.closest('[data-checkgroup]')"):
            continue
        q = question_of(cb)
        pick = choose_option(q, ['Yes', 'No']) if q.strip() else None
        state = cb.evaluate(r"""c => {
            const aria = c.getAttribute('aria-checked');
            return c.indeterminate || (aria !== null && aria !== String(c.checked)) ? null : c.checked;
        }""")
        if pick is None or state is not (pick == 0):
            issues.append(q or 'checkbox')
    return issues


def fill_checkgroups(scope: Locator) -> list[str]:
    """Select a complete confirmed set, then verify the whole group, not just the last click."""
    unanswered = []
    for i, (q, opts, _any_checked) in enumerate(scope.evaluate(CHECKGROUP_JS)):
        picks = checkbox_choices(q, opts)
        if picks is None:
            unanswered.append(q or 'checkbox group')
            continue
        group = scope.locator(f"[data-checkgroup='{i}']")
        try:
            state = group.evaluate(CHECKGROUP_STATE_JS)
            if (state.get('question') != q or state.get('options') != opts
                    or len(state.get('states', [])) != len(opts)
                    or any(type(value) is not bool for value in state['states'])
                    or len(state.get('disabled', [])) != len(opts)
                    or any(state['disabled'][index] for index in picks)):
                unanswered.append(q or 'checkbox group')
                continue
            for index in range(len(opts)):
                state = group.evaluate(CHECKGROUP_STATE_JS)
                if state.get('question') != q or state.get('options') != opts:
                    raise ValueError('checkbox options changed during selection')
                wanted = index in picks
                cb = group.locator("input[type='checkbox']").nth(index)
                if cb.is_checked() == wanted:
                    continue
                if cb.is_disabled() or cb.evaluate(NON_SUBMITTING_CONTROL_JS) is not True:
                    raise UnsafeFormControl('unsafe or disabled checkbox control')
                try:
                    cb.set_checked(wanted, timeout=2000)
                except PWTimeout:
                    if cb.is_checked() != wanted:
                        # Hidden native inputs may have a working visible label/row.
                        row = cb.locator("xpath=ancestor::*[normalize-space(.)!=''][1]")
                        click_form_control(row, timeout=2000)
                        if cb.is_checked() != wanted:
                            cb.set_checked(wanted, force=True, timeout=2000)
            state = group.evaluate(CHECKGROUP_STATE_JS)
            if (state.get('question') != q or state.get('options') != opts
                    or not checkbox_group_confirmed(state, picks)):
                unanswered.append(q or 'checkbox group')
        except Exception as e:
            if browser_closed(e):
                raise BrowserClosed() from None
            log(f"    ! checkbox selection unconfirmed: {e.__class__.__name__}")
            unanswered.append(q or 'checkbox group')
    return unanswered


def standard_answer(el: Locator, q: str) -> str | None:
    """Resolve standard external fields such as name, email and phone."""
    if SENSITIVE_RE.search(q) or NARRATIVE_RE.search(q):
        return None
    key = " ".join([q, el.get_attribute("name") or "", el.get_attribute("id") or "",
                    el.get_attribute("autocomplete") or ""]).lower()
    for pattern, field in STANDARD_FIELDS:
        if re.search(pattern, key):
            return PROFILE.get(field) or None
    return None


TEXT_FIELDS = (
    "input[type='text']:visible, input[type='tel']:visible, input[type='email']:visible, "
    "input[type='number']:visible, input[type='url']:visible, input[type='date']:visible, "
    "input:not([type]):visible, textarea:visible"
)


def text_field_candidates(el: Locator, q: str, *, external: bool) -> list[str]:
    # An exact user-confirmed answer also overrides generic identity-field rules.
    if any(key.strip().casefold() == q.strip().casefold() for key in CFG.get('confirmed_answers', {})):
        return answer(q, numeric=el.get_attribute('type') == 'number')
    std = standard_answer(el, q) if external else None
    if std:
        return [std]
    numeric = el.get_attribute('type') == 'number' or 'numeric' in (el.get_attribute('id') or '')
    return answer(q, numeric=numeric)


def text_value_confirmed(value: str, candidates: list[str], *, numeric: bool = False) -> bool:
    """Accept only a known value, not merely nonempty/native-valid text."""
    value = value.replace('\r\n', '\n').strip()
    if not value:
        return False
    if numeric:
        number = re.compile(r'\d+(?:\.\d+)?')
        return bool(number.fullmatch(value)) and any(
            number.fullmatch(candidate.strip()) and Decimal(value) == Decimal(candidate.strip())
            for candidate in candidates)
    return any(value == candidate.replace('\r\n', '\n').strip() for candidate in candidates)


def unconfirmed_text_questions(scope: Locator, *, external: bool) -> list[str]:
    """Read values again after other fields/uploads; never silently repair prefills."""
    issues = []
    for el in scope.locator(TEXT_FIELDS).all():
        if el.is_disabled():
            continue  # Disabled controls are not submitted by native forms.
        q = question_of(el)
        candidates = text_field_candidates(el, q, external=external)
        if not text_value_confirmed(el.input_value(), candidates, numeric=el.get_attribute('type') == 'number'):
            issues.append(q or 'text field')
    return issues


def fill_step(page: Page, modal: Locator, external: bool = False) -> list[str]:
    """Fill empty fields and verify answers; nonempty text is not confirmation."""
    unanswered: list[str] = []
    # Text fields.
    fields = modal.locator(TEXT_FIELDS)
    for el in fields.all():
        try:
            if not el.is_editable(timeout=2000):
                continue
            q = question_of(el)
            cands = text_field_candidates(el, q, external=external)
            numeric = el.get_attribute('type') == 'number'
            if el.input_value().strip():
                if not text_value_confirmed(el.input_value(), cands, numeric=numeric):
                    unanswered.append(q or 'text field')
                continue
            if external and el.get_attribute("role") == "combobox":
                if not fill_combobox(page, el, q) or not text_value_confirmed(el.input_value(), cands, numeric=numeric):
                    unanswered.append(q)
                continue
            if not cands:
                log(f"    ? unanswered question: {q[:90]}")
                unanswered.append(q)
                continue
            el.fill(cands[0])
            pause(0.6, 1.2)
            # Never select the first typeahead suggestion without a matching answer.
            sugg = page.locator("[role='listbox'] [role='option']:visible")
            if sugg.count():
                idx = match_option(cands, sugg.all_inner_texts())
                if idx is None:
                    unanswered.append(q)
                else:
                    click_form_control(sugg.nth(idx))
            if not text_value_confirmed(el.input_value(), cands, numeric=numeric):
                unanswered.append(q or 'text field')
        except Exception as e:
            if browser_closed(e):
                raise BrowserClosed() from None
            try:
                q = question_of(el)[:70]
            except Exception:
                q = "?"
            log(f"    ! field could not be filled ({e.__class__.__name__}): {q}")
            unanswered.append(q or "text field")

    # Native dropdowns.
    for sel in modal.locator("select:visible").all():
        try:
            options = sel.locator("option").all_inner_texts()
            idx = choose_option(question_of(sel), options)
            if idx is not None:
                sel.select_option(index=idx)
            else:
                unanswered.append(question_of(sel) or "select")
        except Exception as e:
            log(f"    ! could not fill select: {e.__class__.__name__}")
            unanswered.append(question_of(sel) or "select")

    unanswered += fill_radios(modal)

    unanswered += fill_checkgroups(modal)
    # Consent and attestations require an explicit question-specific rule.
    for cb in modal.locator("input[type='checkbox']:visible").all():
        if cb.evaluate("e => !!e.closest('[data-checkgroup]')"):
            continue
        if cb.get_attribute("id") == "follow-company-checkbox":
            continue
        q = question_of(cb)
        idx = choose_option(q, ["Yes", "No"])
        if idx is None:
            unanswered.append(q or "checkbox")
            continue
        try:
            cb.set_checked(idx == 0)
        except Exception:
            unanswered.append(q or "checkbox")

    resume = HOME / PROFILE["resume_file"]
    if external:
        unanswered += fill_yesno(modal)
        # Upload the CV on external forms; skip cover-letter file inputs.
        for fi in modal.locator("input[type='file']").all():
            key = (question_of(fi) + " " + (fi.get_attribute("name") or "") + " " + (fi.get_attribute("id") or "")).lower()
            if re.search(r"cover", key) or not resume.exists():
                continue
            if re.search(r"resume|\bcv\b|lebenslauf|özgeçmiş", key) or modal.locator("input[type='file']").count() == 1:
                try:
                    fi.set_input_files(str(resume))
                    pause(2, 3)
                except Exception as e:
                    log(f"    ! could not upload CV: {e.__class__.__name__}")
                    unanswered.append("resume upload")
        return unanswered

    # Upload the selected CV if it is not listed on LinkedIn (uploads are auto-selected).
    if resume.exists() and modal.locator("input[type='file']").count():
        if resume.name.lower() not in modal.inner_text().lower():
            try:
                modal.locator("input[type='file']").first.set_input_files(str(resume))
                pause(2, 3)
            except Exception:
                unanswered.append("resume upload")
    return unanswered


MODAL = "dialog[open], div[role='dialog']"
DISCARD_RE = re.compile(r"^(discard|sil|vazgeç)$", re.I)


def has_errors(modal: Locator) -> bool:
    return modal.locator(
        "[aria-invalid='true']:visible, [role='alert']:visible, "
        ".artdeco-inline-feedback--error:visible, [data-test-form-element-error-messages]:visible"
    ).count() > 0


def discard(page: Page) -> None:
    try:
        page.keyboard.press("Escape")
        pause(1, 2)
        btn = page.get_by_role("button", name=DISCARD_RE)
        if btn.count():
            click_form_control(btn.first, timeout=4000)
    except Exception:
        pass
    pause(1, 2)


LINKEDIN_SENT_RE = re.compile(
    r"^(?:(?:your )?application (?:was |has been )?(?:sent|submitted)(?: successfully)?"
    r"(?:\s+to\b.*)?|başvurunuz (?:gönderildi|iletildi|alındı))[.!]?$", re.I)


def linkedin_submission_signals(page: Page) -> set[str]:
    """Explicit confirmation lines only, not generic 'thanks' or job descriptions."""
    texts = page.locator(
        "dialog[open]:visible, [role='dialog']:visible, .artdeco-toast-item:visible, [role='status']:visible"
    ).all_inner_texts()
    return {line.strip().casefold() for text in texts for line in text.splitlines()
            if LINKEDIN_SENT_RE.fullmatch(line.strip())}


def wait_linkedin_submission(page: Page, modal: Locator, before: set[str], timeout_seconds: float = 25,
                             expected_url: str | None = None) -> str:
    deadline = time.monotonic() + timeout_seconds
    while True:
        if expected_url is not None and page.url != expected_url:
            return "submission_unconfirmed"
        # A pre-existing toast or a closed modal alone is not proof of submission.
        if linkedin_submission_signals(page) - before:
            return "applied"
        if modal.locator(
            "[aria-invalid='true']:visible, .artdeco-inline-feedback--error:visible, "
            "[data-test-form-element-error-messages]:visible"
        ).count():
            return "submission_rejected"
        alerts = modal.locator("[role='alert']:visible").all_inner_texts()
        if any(re.search(r"error|failed|unable|could not|something went wrong|required|try again|hata|zorunlu", a, re.I)
               for a in alerts):
            return "submission_unconfirmed"
        if time.monotonic() >= deadline:
            return "submission_unconfirmed"
        pause(0.5, 0.5)


def submit_linkedin(page: Page, modal: Locator, primary: Locator, job_id: str,
                    title: str, company: str, applied: dict | None = None) -> str:
    if STOP.is_set():
        raise Stopped
    expected_url = page.url
    before = linkedin_submission_signals(page)
    url = f"https://www.linkedin.com/jobs/view/{job_id}/"
    status = submit_application(page, primary, job_id, title, company, url,
        lambda: wait_linkedin_submission(page, modal, before, expected_url=expected_url), applied)
    if status == "applied":
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
    return status


def submit_application(page: Page, primary: Locator, job_id: str, title: str, company: str,
                       url: str, wait_result, applied: dict | None = None) -> str:
    """Durably track every intentional final submit, even on timeout/STOP/crash."""
    if STOP.is_set():
        raise Stopped
    if DRY_RUN:
        return preview_application(job_id)  # Defense in depth, including the preview budget.
    state = applied if applied is not None else load_applied()
    previous = state.get(job_id)
    if previous and not can_retry(previous["status"]):
        if previous["status"].startswith("applied"):
            return "applied_previously"
        if previous["status"] in UNCERTAIN_STATUSES:
            record_manual(job_id, title, company, url, "previous uncertain attempt: check application history before retrying")
            return "submission_unconfirmed"
        return previous["status"]
    source = source_for_job(job_id)
    denied = reserve_submission_attempt(source)
    if denied:
        return denied
    # Persist before clicking: even a process crash must not cause a blind retry.
    try:
        record(state, job_id, title, company, "submission_pending", url)
    except BaseException:
        release_submission_attempt(source)
        raise
    status = "submission_unconfirmed"
    click_invoked = False
    try:
        if STOP.is_set():
            raise Stopped
        click_invoked = True
        primary.click()
        result = wait_result()
        if result in ("applied", "applied_external", "submission_rejected"):
            status = result
    except Exception as e:
        if browser_closed(e):
            raise BrowserClosed() from None
        log(f"  ! submission outcome unknown: {e.__class__.__name__}")
    finally:
        if not click_invoked:
            release_submission_attempt(source)
        # Also runs for STOP/BrowserClosed (BaseException), preserving uncertainty.
        record(state, job_id, title, company, status, url)
        if status not in ("applied", "applied_external"):
            screenshot(page, f"{job_id.replace(':', '_')}_{status}")
            record_manual(job_id, title, company, url, status + ": check application history before retrying")
    return status


APPLICATION_SENT_RE = re.compile(
    r"^(?:(?:your )?application (?:was |has been )?(?:sent|submitted|received)(?: successfully)?(?:\s+to\b.*)?|"
    r"you (?:have )?(?:successfully )?applied(?:\s+to\b.*)?|thank you for (?:applying|your application)|"
    r"başvurunuz (?:gönderildi|iletildi|alındı))[.!]?$", re.I)


def application_submission_signals(page: Page) -> set[str]:
    texts = page.locator("[role='dialog']:visible, [role='status']:visible, [role='alert']:visible, "
        "main h1:visible, main h2:visible, [data-testid='application-confirmation']:visible").all_inner_texts()
    return {line.strip().casefold() for text in texts for line in text.splitlines()
            if APPLICATION_SENT_RE.fullmatch(line.strip())}


def wait_application_submission(page: Page, before: set[str], checks: int = 25,
                                 expected_host: str | None = None) -> str:
    for _ in range(checks):
        if expected_host is not None and urlparse(page.url).hostname != expected_host:
            return "submission_unconfirmed"
        if application_submission_signals(page) - before:
            return "applied"
        if page.locator("[aria-invalid='true']:visible, [class*='error' i]:visible:has-text('required')").count():
            return "submission_rejected"
        if page.locator(CAPTCHA_SEL).count():
            return "submission_unconfirmed"
        pause(1, 1)
    return "submission_unconfirmed"


def apply_to_job(page: Page, job_id: str, title: str = "", company: str = "", applied: dict | None = None) -> str:
    btn = page.get_by_role("button", name=EASY_APPLY_RE).first
    if not btn.count():
        return "no_easy_apply"
    try:
        click_form_control(btn)
    except UnsafeFormControl:
        record_manual(job_id, title, company, page.url, "submit-capable Easy Apply opener")
        return "manual_review"
    modal = page.locator(MODAL).first
    try:
        modal.wait_for(timeout=10000)
        modal.locator("footer button").first.wait_for(timeout=20000)  # Wait for the form-loading spinner.
    except PWTimeout:
        screenshot(page, f"{job_id}_notloaded")
        discard(page)
        return "modal_not_opened"

    for _ in range(15):
        pause(1, 2)
        unanswered = fill_step(page, modal)
        if require_manual_review(page, modal, unanswered, job_id, title, company):
            discard(page)
            return "manual_review"
        follow = modal.locator("input#follow-company-checkbox:checked")
        if follow.count():
            try:
                click_form_control(modal.locator("label[for='follow-company-checkbox']"))
            except UnsafeFormControl:
                require_manual_review(page, modal, ["unsafe follow-company control"], job_id, title, company)
                discard(page)
                return "manual_review"

        primary = modal.locator("footer button:visible").last
        if not primary.count():
            screenshot(page, f"{job_id}_nobutton")
            discard(page)
            return "skipped_no_button"
        action = application_button_action(primary)

        if action == "submit":
            if DRY_RUN:
                screenshot(page, f"{job_id}_dryrun")
                discard(page)
                return preview_application(job_id)
            return submit_linkedin(page, modal, primary, job_id, title, company, applied)

        if action != "next":
            require_manual_review(page, modal, ["unrecognized application action"], job_id, title, company)
            discard(page)
            return "manual_review"

        before = modal.inner_text()
        try:
            click_navigation(primary)
        except UnsafeFormControl:
            require_manual_review(page, modal, ["submit-capable navigation control"], job_id, title, company)
            discard(page)
            return "manual_review"
        pause(1.5, 3)
        if has_errors(modal) or modal.inner_text() == before:
            unanswered = fill_step(page, modal)  # Retry filling once.
            if require_manual_review(page, modal, unanswered, job_id, title, company):
                discard(page)
                return "manual_review"
            # This locator may now refer to a different step's final button.
            # Re-enter the guarded branch instead of treating it as old Next.
            if application_button_action(primary) != "next":
                continue
            try:
                click_navigation(primary)
            except UnsafeFormControl:
                require_manual_review(page, modal, ["submit-capable navigation control"], job_id, title, company)
                discard(page)
                return "manual_review"
            pause(1.5, 3)
            if has_errors(modal) or modal.inner_text() == before:
                screenshot(page, f"{job_id}_unanswered")
                discard(page)
                return "skipped_unanswered"

    screenshot(page, f"{job_id}_toolong")
    discard(page)
    return "skipped_too_many_steps"


# ---------------------------------------------------------------- external (company sites)

ATS = {
    "greenhouse": frozenset({"boards.greenhouse.io", "boards.eu.greenhouse.io",
                             "job-boards.greenhouse.io", "job-boards.eu.greenhouse.io"}),
    "lever": frozenset({"jobs.lever.co", "jobs.eu.lever.co"}),
    "ashby": frozenset({"jobs.ashbyhq.com"}),
}


class UntrustedApplicationSite(Exception):
    """The application left its approved ATS document or has an unsafe target."""


def safe_https_url(url: str) -> bool:
    """Reject URLs whose interpretation could differ between Python and Chrome."""
    if not isinstance(url, str) or not url or "\\" in url or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in url):
        return False
    try:
        parsed = urlparse(url)
        return (parsed.scheme == "https" and bool(parsed.hostname)
                and parsed.username is None and parsed.password is None
                and parsed.port in (None, 443))
    except ValueError:
        return False


def ats_provider(url: str) -> str | None:
    """Only exact, approved HTTPS hostnames identify an ATS, never paths/queries."""
    if not safe_https_url(url):
        return None
    host = urlparse(url).hostname
    return next((name for name, hosts in ATS.items() if host in hosts), None)


def trusted_ats_document(page: Page, ats: str) -> Locator:
    """Bind all data-entry locators to one verified document, not future navigations.

    Checking page.url alone is racy: Playwright can wait for an element on a new,
    untrusted page. A random document marker prevents that selector from matching.
    """
    url = page.url
    if ats not in ATS or ats_provider(url) != ats:
        raise UntrustedApplicationSite("untrusted_redirect")
    token = secrets.token_hex(16)
    try:
        marked = page.evaluate("""({url, token}) => {
            if (window.location.href !== url) return false;
            document.documentElement.setAttribute('data-autoapply-document', token);
            return true;
        }""", {"url": url, "token": token})
    except Exception as e:
        if browser_closed(e):
            raise BrowserClosed() from None
        raise UntrustedApplicationSite("cannot_verify_document") from e
    if marked is not True:
        raise UntrustedApplicationSite("document_changed")
    return page.locator(f'html[data-autoapply-document="{token}"]')


def check_ats_form(page: Page, document: Locator, form: Locator, ats: str) -> None:
    if ats not in ATS or ats_provider(page.url) != ats or document.count() != 1:
        raise UntrustedApplicationSite("document_changed_or_untrusted_redirect")
    # Check native form targets, including submitter overrides and controls whose
    # form= attribute references a different form. JS API requests remain ATS code.
    try:
        targets = form.evaluate("""root => {
            const controls = [...root.querySelectorAll('input, button, select, textarea')];
            const forms = new Set(root.matches('form') ? [root] : root.querySelectorAll('form'));
            for (const el of controls) if (el.form) forms.add(el.form);
            return [...forms].map(f => f.action || window.location.href).concat(
                controls.filter(e => e.hasAttribute('formaction')).map(e => e.formAction));
        }""")
    except Exception as e:
        if browser_closed(e):
            raise BrowserClosed() from None
        raise UntrustedApplicationSite("cannot_verify_form_targets") from e
    if any(ats_provider(target) != ats for target in targets):
        raise UntrustedApplicationSite("untrusted_form_target")


APPLY_BTN_RE = re.compile(r"\bapply\b|başvur", re.I)
SUBMIT_RE = re.compile(r"submit|send application|apply|başvur|gönder", re.I)
CAPTCHA_SEL = "iframe[src*='hcaptcha'][title*='challenge'], iframe[src*='recaptcha'][title*='challenge']:visible"


def record_manual(job_id: str, title: str, company: str, url: str, reason: str) -> None:
    new_file = not MANUAL_FILE.exists()
    with MANUAL_FILE.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["date", "job_id", "title", "company", "reason", "apply_url"])
        w.writerow([dt.date.today().isoformat(), job_id, title, company, reason, url])


def require_manual_review(page: Page, form: Locator, unanswered: list[str], job_id: str,
                          title: str, company: str, url: str = "", *, external: bool = False) -> bool:
    """Fail closed before advancing/submitting, including when form inspection fails.

    Unknown questions are conservative: even optional/prefilled text needs confirmation.
    Native validation also catches unsupported required fields and failed file uploads.
    """
    issues = list(unanswered)
    try:
        issues += unconfirmed_text_questions(form, external=external)
        issues += unconfirmed_radio_questions(form)
        issues += unconfirmed_checkbox_questions(form)
        issues += form.locator(
            "input, textarea, select, [aria-required='true']:visible"
        ).evaluate_all(r"""els => els.filter(e => {
            if (e.disabled || e.readOnly || e.type === 'hidden') return false;
            if (e.validity && !e.validity.valid) return true;
            if (e.getAttribute('aria-invalid') === 'true') return true;
            if (e.getAttribute('aria-required') !== 'true') return false;
            if (e.type === 'checkbox' || e.type === 'radio') return !e.checked;
            if (e.type === 'file') return !e.files.length;
            if (e.getAttribute('role') === 'radiogroup')
                return !e.querySelector('[aria-checked="true"], input:checked');
            if ('value' in e) return !e.value.trim();
            return !e.getAttribute('aria-valuetext');
        }).map(e => e.getAttribute('aria-label') || e.name || e.id || 'invalid required field')""")
    except Exception as e:
        if browser_closed(e):
            raise BrowserClosed() from None
        issues.append("form inspection failed")
    if not issues:
        return False
    issues = list(dict.fromkeys(q or "unknown question" for q in issues))
    log("  ? manual review: " + " | ".join(issues)[:300])
    screenshot(page, f"{job_id.replace(':', '_')}_manual_review")
    record_manual(job_id, title, company, url or page.url, "unanswered: " + " | ".join(issues)[:300])
    return True


def external_url(page: Page) -> str | None:
    """Follow LinkedIn's external Apply action and return the company-site URL."""
    btn = page.get_by_role("button", name=APPLY_BTN_RE).or_(page.get_by_role("link", name=APPLY_BTN_RE))
    return follow_apply(page, btn.filter(has_not_text=EASY_APPLY_RE).first)


def follow_apply(page: Page, btn: Locator) -> str | None:
    """Click Apply and return the destination URL from navigation or a new tab."""
    if not btn.count():
        return None
    ctx = page.context
    before, start = len(ctx.pages), page.url
    try:
        click_form_control(btn)
    except UnsafeFormControl:
        return None
    for _ in range(30):
        time.sleep(0.5)
        if page.url != start and urlparse(page.url).netloc != urlparse(start).netloc:
            url = page.url
            page.go_back(wait_until="domcontentloaded")
            return url
        if len(ctx.pages) > before:
            new = ctx.pages[-1]
            try:
                new.wait_for_load_state("domcontentloaded", timeout=20000)
                pause(2, 3)
            except PWTimeout:
                pass
            url = new.url
            new.close()
            return url
        # Intermediate "Share your profile / Continue" dialog.
        cont = page.locator(MODAL).get_by_role("button", name=re.compile(r"continue|devam", re.I))
        if cont.count():
            try:
                click_form_control(cont.first)
            except UnsafeFormControl:
                return None
    return None


def ats_apply_url(page: Page, url: str) -> tuple[str | None, str]:
    """Find a direct application URL/provider, including embedded company-site forms."""
    name = ats_provider(url)
    if not name:
        if not safe_https_url(url):
            return None, "unknown"
        # Discover an approved Greenhouse/Ashby/Lever iframe on the company site.
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            pause(3, 4)
        except Exception:
            return None, "unknown"
        if not safe_https_url(page.url):
            return None, "unknown"
        name = ats_provider(page.url)
        if name:
            url = page.url
        else:
            for fr in page.frames:
                name = ats_provider(fr.url)
                if name:
                    url = fr.url
                    break
            else:
                return None, "unknown"
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    if name == "lever" and not path.endswith("/apply"):
        path += "/apply"
    elif name == "ashby" and not path.endswith("/application"):
        path += "/application"
    url = parsed._replace(path=path or "/").geturl()
    return url, name


EXT_STATE = {"count": 0, "last": 0.0}


def apply_external(page: Page, job_id: str, title: str, company: str, applied: dict | None = None) -> str:
    url = external_url(page)
    if not url:
        return "no_apply_button"
    return apply_form(page, job_id, title, company, url, applied)


def apply_form(page: Page, job_id: str, title: str, company: str, url: str, applied: dict | None = None) -> str:
    """Process a company-site form with a per-run quota and inter-attempt cooldown."""
    ext = CFG["external"]
    if not EXTERNAL:
        record_manual(job_id, title, company, url, "external_disabled")
        return "external_manual"
    denied = submission_quota_status(source_for_job(job_id))
    if denied:
        return denied
    if EXT_STATE["count"] >= ext.get("max_per_run", 5):
        return "skipped_external_limit"  # May be retried on the next run.
    form_url, ats = ats_apply_url(page, url)
    if not form_url:
        record_manual(job_id, title, company, url, "unsupported_site")
        return "external_manual"
    wait = EXT_STATE["last"] + ext.get("min_wait_seconds", 120) - time.time()
    if wait > 0:
        log(f"  .. waiting {int(wait)} seconds before the next company form")
        pause(wait, wait)
    EXT_STATE["count"] += 1
    EXT_STATE["last"] = time.time()
    log(f"  -> {ats}: {form_url[:90]}")
    page.goto(form_url, wait_until="domcontentloaded", timeout=30000)
    pause(3, 5)
    try:
        return apply_trusted_form(page, job_id, title, company, form_url, ats, applied)
    except (UntrustedApplicationSite, UnsafeFormControl) as e:
        log(f"  ! unsafe application site/action: {e}")
        record_manual(job_id, title, company, url, str(e))
        return "external_manual"


def apply_trusted_form(page: Page, job_id: str, title: str, company: str, form_url: str, ats: str,
                       applied: dict | None = None) -> str:
    document = trusted_ats_document(page, ats)
    if ats == "ashby":
        tab = document.get_by_role("tab", name=re.compile(r"application", re.I))
        if tab.count():
            click_form_control(tab.first)
            pause(1.5, 2.5)
    form = document.locator("form:has(input[type='file']), form:has(textarea), form:has(input[type='email'])").first
    if not form.count():
        form = document.locator("body")

    check_ats_form(page, document, form, ats)
    unanswered = fill_step(page, form, external=True)
    pause(1, 2)
    check_ats_form(page, document, form, ats)
    unanswered += fill_step(page, form, external=True)  # Second pass for conditional questions.
    check_ats_form(page, document, form, ats)
    if require_manual_review(page, form, unanswered, job_id, title, company, form_url, external=True):
        return "manual_review"

    submit = form.locator("button[type='submit']:visible, input[type='submit']:visible")
    if not submit.count():
        submit = form.get_by_role("button", name=SUBMIT_RE)
    if not submit.count():
        screenshot(page, f"{job_id}_ext_nosubmit")
        record_manual(job_id, title, company, form_url, "no_submit_button")
        return "external_manual"
    if DRY_RUN:
        screenshot(page, f"{job_id}_ext_dryrun")
        return preview_application(job_id)

    check_ats_form(page, document, form, ats)
    before = application_submission_signals(page)
    def wait_result():
        result = wait_application_submission(page, before, expected_host=urlparse(form_url).hostname)
        return "applied_external" if result == "applied" else result
    return submit_application(page, submit.last, job_id, title, company, form_url, wait_result, applied)


# ---------------------------------------------------------------- search

def search_url(keyword: str, location: str) -> str:
    s = CFG["search"]
    params = {
        "keywords": keyword,
        "location": location,
        "f_TPR": f"r{s['posted_within_hours'] * 3600}",
        "sortBy": "DD",
    }
    if not EXTERNAL:
        params["f_AL"] = "true"  # Easy Apply only.
    if s.get("remote_only"):
        params["f_WT"] = "2"
    if s.get("experience_levels"):
        params["f_E"] = ",".join(map(str, s["experience_levels"]))
    return "https://www.linkedin.com/jobs/search/?" + urlencode(params)


SCRAPE_IDS_JS = """async () => {
    const cards = [...document.querySelectorAll('[data-occludable-job-id], [data-job-id]')];
    for (const c of cards) { c.scrollIntoView(); await new Promise(r => setTimeout(r, 120)); }
    const ids = cards.map(c => c.getAttribute('data-occludable-job-id') || c.getAttribute('data-job-id'));
    for (const a of document.querySelectorAll('a[href*="/jobs/view/"], a[href*="currentJobId="]')) {
        const m = a.href.match(/jobs\\/view\\/(\\d+)|currentJobId=(\\d+)/);
        if (m) ids.push(m[1] || m[2]);
    }
    return ids;
}"""


def open_list(page: Page, url: str) -> list[str] | None:
    try:
        page.goto(url, wait_until="domcontentloaded")
    except Exception as e:
        if "TOO_MANY_REDIRECTS" in str(e):
            raise SessionLost() from None
        if browser_closed(e):
            raise BrowserClosed() from None
        log(f"  ! could not open page: {str(e)[:100]}")
        return None
    pause(3, 5)
    return list(dict.fromkeys(f for f in page.evaluate(SCRAPE_IDS_JS) if f and f.isdigit()))


def job_lists(page: Page):
    """Search only the regions selected by the user, not personalized feeds."""
    locations = selected_locations()
    for kw in CFG["search"]["keywords"]:
        for loc in locations:
            for p in range(CFG["search"]["pages_per_search"]):
                found = open_list(page, search_url(kw, loc) + f"&start={p * 25}")
                if found is None:
                    break
                log(f"  '{kw}' @ {loc}, page {p + 1}: {len(found)} jobs")
                yield found
                if len(found) < 25:
                    break


def first_text(page: Page, selectors: list[str]) -> str:
    for sel in selectors:
        loc = page.locator(sel).first
        if loc.count():
            try:
                return loc.inner_text(timeout=3000).strip()
            except PWTimeout:
                pass
    return ""


DESC_JS = """() => {
    const h = [...document.querySelectorAll('h2')].find(e => /about the job|ilanı hakkında/i.test(e.innerText));
    const d = h ? h.parentElement : document.querySelector('#job-details, .jobs-description');
    return d ? d.innerText : '';
}"""


def job_info(page: Page) -> tuple[str, str, str]:
    """Use the current job card, never an assumed tab-title order."""
    details = {}
    for attempt in range(3):
        details = page.evaluate(job_metadata.DETAILS_JS)
        if details.get('title') and details.get('company'):
            break
        if attempt < 2:
            page.wait_for_timeout(1000)
    title, company, description = details.get('title', ''), details.get('company', ''), details.get('description', '')
    if not title or not company or not description:
        structured = job_metadata.page_metadata(page, 'linkedin', page.url, attempts=1)
        title = title or structured.get('title', '')
        company = company or structured.get('company', '')
        description = description or html.unescape(re.sub(r'<[^>]+>', ' ', structured.get('description', '') or ''))
    return title, company, description


def job_ok(title: str, company: str, description: str) -> str | None:
    """Return None for an eligible job, otherwise the rejection reason."""
    f = CFG["filters"]
    t, d = title.lower(), description.lower()
    if not title.strip() or not company.strip():
        return 'identity_unknown'
    if not any(re.search(p, t) for p in f["title_include"]):
        return "title_not_relevant"
    if not all(re.search(p, t) for p in f.get("title_require", [])):
        return "title_not_relevant"
    if any(re.search(p, t) for p in f["title_exclude"]):
        return "title_excluded"
    if any(re.search(p, d) for p in f.get("description_exclude", [])):
        return "description_excluded"
    if any(c.lower() in company.lower() for c in f.get("exclude_companies", [])):
        return "company_excluded"
    return job_relevance.reason(title, description, **technology_filters())


def technology_filters() -> dict:
    """Only explicitly saved user lists authorize technology filtering."""
    job_relevance.validate_settings(SETTINGS)
    return {field: list(SETTINGS.get(field, [])) for field in job_relevance.FIELDS}


def read_job_metadata(page: Page, source: str, url: str) -> dict:
    try:
        result = job_metadata.page_metadata(page, source, url)
        return result or {'diagnostic': {'reason': 'Requested job URL no longer matches the document (redirect/login/different job)'}}
    except Exception as e:
        if browser_closed(e):
            raise BrowserClosed() from None
        log(f"  ? job metadata unavailable: {e.__class__.__name__}")
        return {'diagnostic': {'reason': 'Metadata inspection failed', 'error': e.__class__.__name__}}


def job_location_ok(text: str) -> bool:
    """Queryless feeds must explicitly match a user-selected region label."""
    return bool(text.strip()) and any(re.search(rf'(?<!\w){re.escape(region)}(?!\w)', text, re.I)
                                      for region in selected_locations())


def check_job_eligibility(job: dict, applied: dict) -> bool:
    """Shared final gate: search URLs and board names are not eligibility evidence."""
    search = CFG["search"]
    reason = job_metadata.eligibility_reason(job.get("metadata") or {}, search["posted_within_hours"],
                                             search.get("remote_only", False))
    # A board's location label is not an employment-eligibility rule. Search
    # regions come from user settings; never infer allowed regions from the CV.
    if reason is None:
        return True
    unknown = reason in ("posting_date_unknown", "posting_date_future", "workplace_unknown", 'location_unknown')
    status = "metadata_manual" if unknown else reason
    log(f"- skip [{reason}] {job['title']} @ {job['company']}")
    if unknown:
        diagnostic = (job.get('metadata') or {}).get('diagnostic')
        record_manual(job["id"], job["title"], job["company"], job["url"], reason + (': ' + json.dumps(diagnostic, ensure_ascii=False) if diagnostic else ''))
    record(applied, job["id"], job["title"], job["company"], status, job["url"])
    if applied.get(job['id'], {}).get('decisions'):
        applied[job['id']]['reason'] = reason
        applied[job['id']]['metadata_diagnostic'] = (job.get('metadata') or {}).get('diagnostic')
        applied[job['id']]['decisions'][-1].update(reason=reason, metadata_diagnostic=applied[job['id']]['metadata_diagnostic'])
        write_applied(applied)
    return False


RECHECK_STATE = {'id': secrets.token_hex(8), 'count': 0}
MANUAL_RELEVANCE = frozenset({'identity_unknown', 'stack_unknown'})


def begin_job_rechecks(applied: dict) -> None:
    RECHECK_STATE.update(id=secrets.token_hex(8), count=0)
    if SETTINGS.get('recheck_enabled', CFG.get('rechecks', {}).get('enabled', False)):
        if job_rechecks.prepare(applied, [APPLIED_FILE, LOG_FILE, MANUAL_FILE], HOME / 'data' / 'backups'):
            write_applied(applied)


def should_visit_job(applied: dict, job_id: str) -> bool:
    previous = applied.get(job_id)
    if previous is None or can_retry(previous['status']):
        return True
    if not SETTINGS.get('recheck_enabled', CFG.get('rechecks', {}).get('enabled', False)):
        return False
    options = CFG.get('rechecks', {})
    if job_rechecks.claim(previous, job_rechecks.revision(CFG, SETTINGS), RECHECK_STATE,
                          max_per_run=options.get('max_per_run', 10), max_per_job=options.get('max_per_job', 3),
                          cooldown_hours=options.get('cooldown_hours', 24)):
        write_applied(applied)
        return True
    return False


def record_relevance_decision(applied: dict, job_id: str, title: str, company: str, reason: str, url: str, description: str = '') -> None:
    if reason in MANUAL_RELEVANCE:
        record_manual(job_id, title, company, url, reason)
    record(applied, job_id, title, company, 'relevance_manual' if reason in MANUAL_RELEVANCE else reason, url)
    if applied.get(job_id, {}).get('decisions'):
        applied[job_id]['reason'] = reason
        applied[job_id]['decisions'][-1]['reason'] = reason
        if reason in ('excluded_technology', 'stack_unknown'):
            evidence = job_relevance.assess(title, description, **technology_filters())
            applied[job_id]['technology_diagnostic'] = evidence
            applied[job_id]['decisions'][-1]['technology_diagnostic'] = evidence
        write_applied(applied)


def mark_evaluation_passed(applied: dict, job_id: str, title: str, company: str, url: str) -> None:
    if applied.get(job_id, {}).get('status') in job_rechecks.RECHECKABLE:
        record(applied, job_id, title, company, 'evaluation_passed', url)


# ---------------------------------------------------------------- startup questions

def ask(prompt: str, default: str) -> str:
    raw = input(f"{prompt} [{default}]: " if default else f"{prompt}: ").strip()
    return raw or default


def ask_settings() -> None:
    """Collect CV, salary and English settings; reuse them as defaults next time."""
    load_settings()
    if not sys.stdin.isatty():
        log("No interactive terminal; using saved settings.")
    else:
        print("\n=== Application details (Enter keeps the value in brackets) ===")
        while True:
            raw = ask("CV file (PDF/DOCX path; you can drag the file into this terminal)", SETTINGS["resume"])
            path = Path(raw.strip("'\" ").replace("\\ ", " ")).expanduser()
            path = path if path.is_absolute() else HOME / path
            if path.is_file() and path.suffix.lower() in (".pdf", ".doc", ".docx"):
                SETTINGS["resume"] = str(path)
                break
            print(f"  ! File not found or not a PDF/DOCX: {path}")
        while (cur := ask("Salary currency (USD/EUR/GBP/TRY)", SETTINGS.get("currency", "USD")).upper()) not in CURRENCY_RE:
            pass
        SETTINGS["currency"] = cur
        while True:
            salary = re.sub(r"[^\d]", "", ask(f"Annual salary expectation ({cur}, digits only)", SETTINGS["salary"]))
            if salary:
                SETTINGS["salary"] = salary
                break
        print("English level:")
        for key, (label, _) in ENGLISH_LEVELS.items():
            print(f"  {key}) {label}")
        while (eng := ask("Choose a level", SETTINGS["english"])) not in ENGLISH_LEVELS:
            pass
        SETTINGS["english"] = eng
        SETTINGS["notice"] = re.sub(r"[^\d]", "", ask("Notice period (days)", SETTINGS["notice"])) or SETTINGS["notice"]
        cv_setup()
        print()
    finalize_settings()


def load_settings() -> dict:
    saved = json.loads(SETTINGS_FILE.read_text(encoding="utf-8")) if SETTINGS_FILE.exists() else {}
    llm_config.resolve(CFG, saved.get('llm_preferences', {}), require_model=False)
    SETTINGS.update({"resume": PROFILE["resume_file"], "salary": "75000", "currency": "USD", "english": "3", "notice": "14", **saved})
    return SETTINGS


def finalize_settings() -> None:
    """Save and apply profile/search settings for both terminal and GUI workflows."""
    if SETTINGS.get("cv_profile") is not None:
        from cv_profile import validate
        validate(SETTINGS["cv_profile"])
    current_llm_config(require_model=False)
    job_relevance.validate_settings(SETTINGS)
    locations = selected_locations()
    SETTINGS_FILE.write_text(json.dumps(SETTINGS, ensure_ascii=False, indent=2), encoding="utf-8")
    PROFILE["resume_file"] = SETTINGS["resume"]
    CFG['search']['locations'] = locations
    if SETTINGS.get("cv_profile"):
        apply_cv_profile(SETTINGS["cv_profile"])
    log(f"CV: {Path(SETTINGS['resume']).name} | salary: {', '.join(f'{v} {c}' for c, v in (SETTINGS.get('monthly_salary_by_currency') or {}).items()) + ' /month' if SETTINGS.get('monthly_salary_by_currency') else SETTINGS['salary'] + ' /year'} | "
        f"English: {ENGLISH_LEVELS[SETTINGS['english']][0]} | notice: {SETTINGS['notice']} days")


def api_key() -> str:
    key = find_key()
    if key:
        return key
    key = input("OpenRouter API key (openrouter.ai/settings/keys; free models only, no paid requests; leave blank to skip): ").strip()
    if key:
        save_key(key)
    return key


def current_llm_config(*, require_model: bool = True, overrides=None) -> llm_config.ResolvedLLMConfig:
    return llm_config.resolve(CFG, SETTINGS.get('llm_preferences', {}),
                              {} if overrides is None else overrides, require_model=require_model)


def save_key(key: str, provider: str = 'openrouter') -> None:
    llm_credentials.save(HOME, provider, key)


def find_key(provider: str = 'openrouter') -> str:
    """Backward-compatible OpenRouter default; other providers stay isolated."""
    return llm_credentials.load(HOME, provider).value


def show_profile(prof: dict, locations: list[str]) -> None:
    print("\n=== Profile extracted from your CV ===")
    print(f"Name: {prof['first_name']} {prof['last_name']} | {prof['email']} | {prof['phone_intl']}")
    print(f"City: {prof['city']} | LinkedIn: {prof['linkedin'] or '-'} | GitHub: {prof['github'] or '-'}")
    print(f"Current title: {prof['current_title']} | Total experience: {prof['total_years']} years")
    print("Job titles to search:")
    for t in prof["job_titles"]:
        print(f"  - {t}")
    print("Title keywords: " + ", ".join(prof["title_keywords"]))
    print("Skill experience: " + ", ".join(f"{x['skill']} {x['years']} years" for x in prof["skills"]))
    print("Countries / regions: " + ", ".join(locations))
    print(f"Cover letter: {prof['cover_letter']}")


def cv_setup() -> None:
    """Extract or reuse a matching CV profile and ask the user to review it."""
    import cv_profile
    resume = Path(SETTINGS["resume"])
    digest = hashlib.sha256(resume.read_bytes()).hexdigest()
    cached = SETTINGS.get("cv_profile") if SETTINGS.get("cv_hash") == digest else None
    prof = dict(cached) if cv_profile.valid(cached) else None
    if not prof:
        config = current_llm_config()
        if not config.legacy_supported:
            raise llm_config.LLMConfigError('Selected LLM adapter is not implemented yet; no fallback was performed')
        key = api_key()
        if not key:
            SETTINGS.pop("cv_profile", None)
            return
        print("Analyzing CV...")
        try:
            prof = cv_profile.extract(resume, key, log=print, max_output_tokens=config.max_output_tokens,
                                      timeout_seconds=config.timeout_seconds)
            cv_profile.validate(prof)
        except Exception as e:
            print(f"  ! Could not analyze CV ({e.__class__.__name__}: {str(e)[:150]}); using config.yaml.")
            SETTINGS.pop("cv_profile", None)
            return
    locations = selected_locations()
    while True:
        show_profile(prof, locations)
        choice = ask("Use this profile? y=yes, t=edit job titles, l=edit locations, n=do not use the CV profile", "y").lower()
        if choice == "y":
            cv_profile.validate(prof)
            SETTINGS.update(cv_profile=prof, cv_hash=digest, locations=locations)
            return
        if choice == "n":
            SETTINGS.pop("cv_profile", None)
            return
        if choice == "t":
            raw = input("Job titles (comma-separated): ")
            candidate = dict(prof)
            candidate["job_titles"] = [t.strip() for t in raw.split(",") if t.strip()] or prof["job_titles"]
            words = {w.lower() for t in candidate["job_titles"] for w in re.findall(r"[\w+#]{3,}", t)}
            candidate["title_keywords"] = sorted(set(prof["title_keywords"]) | words)
            try:
                cv_profile.validate(candidate)
                prof = candidate
            except cv_profile.ProfileValidationError as e:
                print(f"  ! {e}")
        if choice == "l":
            raw = input("Countries / regions (comma-separated, e.g. European Union, United Kingdom): ")
            locations = [t.strip() for t in raw.split(",") if t.strip()] or locations


def selected_locations() -> list[str]:
    """Use the user's list verbatim; config is used only if no list was saved."""
    locations = SETTINGS['locations'] if 'locations' in SETTINGS else CFG['search']['locations']
    if not isinstance(locations, list) or not locations or any(not isinstance(loc, str) or not loc.strip() for loc in locations):
        raise ValueError('Enter at least one search country / region')
    return list(locations)


def apply_cv_profile(prof: dict) -> None:
    """Apply the reviewed CV profile to search settings, filters and form answers."""
    from cv_profile import validate
    validate(prof)
    for k in ("first_name", "last_name", "email", "phone_intl", "city", "country", "current_title", "cover_letter"):
        PROFILE[k] = prof[k]  # Empty validated fields must not retain another CV's answers.
    # Missing CV links must not reuse links from another profile in the config.
    PROFILE.update(linkedin=prof["linkedin"], github=prof["github"], current_company="")
    for k in ("linkedin", "github"):
        if PROFILE[k] and not PROFILE[k].startswith("http"):
            PROFILE[k] = "https://" + PROFILE[k]
    PROFILE["full_name"] = f"{PROFILE['first_name']} {PROFILE['last_name']}".strip()
    PROFILE.update(phone="", country_code="")
    if prof.get("phone_intl"):
        # Derive the local number when country code is a separate field: "+90 534 ..." -> "534...".
        PROFILE["phone"] = re.sub(r"\D", "", re.sub(r"^\+\d{1,3}[\s-]", "", prof["phone_intl"]))
        code = re.match(r"\+(\d{1,3})[\s-]", prof["phone_intl"])
        PROFILE["country_code"] = f"(+{code.group(1)})" if code else ""
    years = prof["total_years"]
    search = CFG["search"]
    search["keywords"] = SETTINGS.get('job_search_titles') or prof["job_titles"]
    search["locations"] = selected_locations()
    search["experience_levels"] = [1, 2] if years < 2 else [2, 3, 4] if years < 6 else [3, 4, 5]
    words = {w for t in prof["job_titles"] for w in re.findall(r"[\w+#]+", t.lower()) if len(w) > 1}
    words |= {w.lower() for w in prof["title_keywords"]}
    CFG["filters"]["title_include"] = ["|".join(rf"\b{re.escape(w)}\b" for w in sorted(words))]
    # Require a role keyword as well to reject unrelated keyword-only job titles.
    roles = {re.findall(r"[\w+#]+", t.lower())[-1] for t in prof["job_titles"] if re.findall(r"[\w+#]+", t)}
    roles |= {"engineer", "developer", "scientist", "analyst", "architect", "researcher", "specialist", "lead"} & words
    CFG["filters"]["title_require"] = ["|".join(rf"\b{re.escape(w)}" for w in sorted(roles))] if roles else []
    # Residence does not establish work authorization or willingness to commute.
    # AI-estimated skill years are not shown for confirmation in the GUI.
    # Keep only the user's explicit skill_years configuration, never CV estimates.
    confirmed = SETTINGS.get('confirmed_skill_years', {})
    if isinstance(confirmed, dict):
        for skill, years_confirmed in confirmed.items():
            if isinstance(skill, str) and skill.strip() and type(years_confirmed) is int and 0 <= years_confirmed <= 80:
                CFG['skill_years'][re.escape(skill.casefold())] = years_confirmed


# ---------------------------------------------------------------- main

def process_job(page: Page, applied: dict, job_id: str) -> bool:
    """Open and process an eligible job; return True for a confirmed success or preview."""
    try:
        previous = applied.get(job_id)
        if previous and not can_retry(previous['status']):
            authorized_recheck = (previous['status'] in job_rechecks.RECHECKABLE
                                 and previous.get('recheck', {}).get('run_id') == RECHECK_STATE['id'])
            if not authorized_recheck:
                return False
        if submission_quota_status('linkedin'):
            return False
        page.goto(f"https://www.linkedin.com/jobs/view/{job_id}/", wait_until="domcontentloaded")
        pause(2.5, 4.5)
        title, company, desc = job_info(page)
        reason = job_ok(title, company, desc) or ("applied_elsewhere" if applied_elsewhere(applied, title, company) else None)
        if reason:
            log(f"- skip [{reason}] {title} @ {company}")
            record_relevance_decision(applied, job_id, title, company, reason, f'https://www.linkedin.com/jobs/view/{job_id}/', desc)
            return False
        url = f"https://www.linkedin.com/jobs/view/{job_id}/"
        job = {"id": job_id, "title": title, "company": company, "url": url, 'desc': desc,
               "metadata": read_job_metadata(page, "linkedin", url)}
        if not check_job_eligibility(job, applied):
            return False
        log(f"> {title} @ {company}")
        mark_evaluation_passed(applied, job_id, title, company, url)
        status = apply_to_job(page, job_id, title, company, applied)
        if status == "no_easy_apply" and EXTERNAL:
            status = apply_external(page, job_id, title, company, applied)
        log(f"  = {status}")
        if status != "dry_run" and applied.get(job_id, {}).get("status") != status:
            record(applied, job_id, title, company, status)
        pause(4, 9)
        return status in ("applied", "applied_external", "dry_run")
    except Exception as e:
        if "TOO_MANY_REDIRECTS" in str(e):
            raise SessionLost() from None
        if browser_closed(e):
            raise BrowserClosed() from None
        log(f"  ! error {job_id}: {e.__class__.__name__}: {str(e)[:150]}")
        screenshot(page, f"{job_id}_error")
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        return False


def linkedin_login(page: Page) -> bool:
    page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded")
    pause(2, 4)
    if re.search(r"login|checkpoint|authwall|signup|uas/", page.url) or page.url.rstrip("/") == "https://www.linkedin.com":
        log("Not signed in. Please sign in to LinkedIn in the open Chrome window (waiting up to 10 minutes)...")
        if "login" not in page.url:
            page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded")
        try:
            page.wait_for_url(re.compile(r"linkedin\.com/(feed|in/|mynetwork|jobs)"), timeout=600_000)
        except PWTimeout:
            log("LinkedIn login was not completed; moving to other sources.")
            return False
        pause(3, 5)
    log("LinkedIn login complete.")
    return True


def run_linkedin(page: Page, applied: dict, count: int) -> int:
    if submission_quota_status('linkedin') or count >= CFG['limits']['max_applications_per_run']:
        return count
    if not linkedin_login(page):
        return count
    stop = min(CFG["limits"]["max_applications_per_run"], count + CFG["limits"].get("per_source", 15))
    visited = set()
    try:
        for job_ids in job_lists(page):
            for job_id in job_ids:
                if job_id in visited:
                    continue
                visited.add(job_id)
                if not should_visit_job(applied, job_id):
                    continue
                if count >= stop or submission_quota_status('linkedin') or time.time() > DEADLINE:
                    raise LimitReached
                with submission_source('linkedin'):
                    if process_job(page, applied, job_id):
                        count += 1
    except LimitReached:
        log("Run application limit or runtime limit reached.")
    except SessionLost:
        log("LinkedIn session expired. Sign in again on the next run; moving to other sources.")
    return count


def launch(pw):
    """Launch a persistent browser profile that retains manual site logins."""
    last = None
    for channel in ("chrome", "msedge"):
        try:
            return pw.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE_DIR),
                channel=channel,
                headless=os.getenv("HEADLESS", "false").lower() == "true",
                locale="en-US",
                viewport={"width": 1366, "height": 900},
                args=["--disable-blink-features=AutomationControlled"],
            )
        except Exception as e:
            last = e
    raise RuntimeError("Google Chrome could not be launched. Install it from https://www.google.com/chrome and try again.") from last


def run_bot(*, reset_stop: bool = True) -> int:
    """Run the application workflow after SETTINGS have been prepared."""
    global DEADLINE
    if SETTINGS.get("cv_profile") is not None:
        from cv_profile import validate
        validate(SETTINGS["cv_profile"])
    DEADLINE = time.time() + CFG["limits"]["max_runtime_minutes"] * 60
    if reset_stop:
        STOP.clear()
    if STOP.is_set():
        log("Stopped before the run started.")
        return 0
    applied = load_applied()
    begin_job_rechecks(applied)
    # Quota is per run; keep 'last' so restarting does not bypass the cooldown.
    EXT_STATE["count"] = 0
    reset_submission_attempts()
    log(f"Starting ({'test mode; applications will not be sent' if DRY_RUN else 'LIVE mode; real applications may be sent'}); previously recorded jobs: {len(applied)}")
    count = 0
    with sync_playwright() as pw:
        ctx = launch(pw)
        # Limit control actions to 6 seconds and page navigation to 30 seconds.
        ctx.set_default_timeout(6000)
        ctx.set_default_navigation_timeout(30000)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            if CFG["sources"].get("linkedin", True):
                count = run_linkedin(page, applied, count)
            import sources
            count = sources.run(sys.modules[__name__], page, applied, count)
        except BrowserClosed:
            log("Browser closed; stopped. Remaining jobs may be processed on the next run.")
        except Stopped:
            log("Stopped.")
        finally:
            try:
                ctx.close()
            except Exception:
                pass
    LAST_RUN_FILE.write_text(f"{dt.datetime.now().isoformat(timespec='minutes')} {'dry-run' if DRY_RUN else 'applied'}={count} attempts={ATTEMPTS['total']}\n", encoding="utf-8")
    log(f"Finished. {'Tested' if DRY_RUN else 'Confirmed applications'}: {count} | {'previews' if DRY_RUN else 'Submit attempts'}: {ATTEMPTS['total']}")
    return 0


def main() -> int:
    from cv_profile import ProfileValidationError
    try:
        ask_settings()
        return run_bot()
    except ProfileValidationError as e:
        log(f"Invalid profile: {e}. Review your details or read the CV again.")
        return 1
    except (llm_config.LLMConfigError, llm_credentials.CredentialError) as e:
        log(f'LLM configuration error: {e}')
        return 1


if __name__ == "__main__":
    sys.exit(main())
