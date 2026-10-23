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

from ats_engine import ATSEngine

from form_engine import FormEngine

from platforms.linkedin import LinkedInAdapter
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
import linkedin_search
import application_answers
import deferred_review
import description_instructions
import linkedin_forms
import application_identity
from platforms.registry import ats_adapter
from run_metrics import RunMetrics

METRICS = RunMetrics()
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

from form_engine import QUESTION_JS


# Only these explicit pre-submission statuses may be retried on a later run.
RETRYABLE_STATUSES = frozenset({"modal_not_opened", "blocked", "skipped_no_button", "skipped_no_form",
                               "skipped_external_limit", "pre_submission_error",
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
    application_identity.reset(sys.modules[__name__])


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
    status = reserve_submission_attempt(source_for_job(job_id)) or 'dry_run'
    if status == 'dry_run':
        METRICS.mark('previews', job_id)
    return status


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
    binding = application_identity.ledger(sys.modules[__name__]).bindings.get(job_id)
    if binding:
        applied[job_id].update(binding)
        if 'application_alias_of' not in binding:
            applied[job_id].pop('application_alias_of', None)
    applied[job_id].pop('metadata_diagnostic', None)
    applied[job_id].pop('technology_diagnostic', None)
    applied[job_id].pop('title_diagnostic', None)
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
    reviewed = application_answers.preset_answer(question, SETTINGS)
    if reviewed:
        return [v for v in reviewed if re.fullmatch(r'\d+(?:\.\d+)?', v)] if numeric else reviewed
    if SENSITIVE_RE.search(q) or NARRATIVE_RE.search(q):
        return []
    contact = application_answers.contact_answers(question, PROFILE)
    if contact:
        return [v for v in contact if re.fullmatch(r'\d+(?:\.\d+)?', v)] if numeric else contact
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
    if application_answers.contact_kind(question) == 'country_code':
        exact = match_option(answer(question), options)
        return exact if exact is not None else application_answers.country_code_option(answer(question), options)
    if YEARS_RE.search(question) or EXPERIENCE_RE.search(question):
        years = answer(question, numeric=True)
        idx = choose_years(float(years[0]), opts) if years else None
        if idx is not None:
            return idx
    return match_option(answer(question), options)


def question_of(el: Locator) -> str:
    return FormEngine(sys.modules[__name__]).question_of(el)


# ---------------------------------------------------------------- form filling

class UnsafeFormControl(ValueError):
    """Only the explicit final-submit path may activate a native submit control."""


from form_engine import UNSAFE_SUBMIT_SELECTOR
from form_engine import NON_SUBMITTING_CONTROL_JS


def click_form_control(control: Locator, timeout: int=5000) -> None:
    return FormEngine(sys.modules[__name__]).click_form_control(control, timeout)


from form_engine import NAVIGATION_BUTTON_RE
from form_engine import FINAL_BUTTON_RE


def application_button_action(button: Locator) -> str:
    return FormEngine(sys.modules[__name__]).application_button_action(button)


def click_navigation(button: Locator) -> None:
    return FormEngine(sys.modules[__name__]).click_navigation(button)


from form_engine import RADIO_GROUP_JS


from form_engine import RADIO_GROUPS
from form_engine import RADIO_SELECTION_JS


def radio_choice_confirmed(states, index) -> bool:
    return FormEngine(sys.modules[__name__]).radio_choice_confirmed(states, index)


def fill_radios(scope: Locator) -> list[str]:
    return FormEngine(sys.modules[__name__]).fill_radios(scope)


# Standard external fields: (question/name/id pattern, profile key); first match wins.
from form_engine import STANDARD_FIELDS


def fill_combobox(page: Page, el: Locator, q: str) -> bool:
    return FormEngine(sys.modules[__name__]).fill_combobox(page, el, q)


from form_engine import YESNO_JS

from form_engine import YESNO_SELECTION_JS

from form_engine import CHECKGROUP_STATE_JS

from form_engine import CHECKGROUP_JS


def fill_yesno(scope: Locator) -> list[str]:
    return FormEngine(sys.modules[__name__]).fill_yesno(scope)


def unconfirmed_radio_questions(scope: Locator) -> list[str]:
    return FormEngine(sys.modules[__name__]).unconfirmed_radio_questions(scope)


def checkbox_choices(question: str, options: list[str]) -> list[int] | None:
    return FormEngine(sys.modules[__name__]).checkbox_choices(question, options)


def checkbox_group_confirmed(state, picks) -> bool:
    return FormEngine(sys.modules[__name__]).checkbox_group_confirmed(state, picks)


def unconfirmed_checkbox_questions(scope: Locator) -> list[str]:
    return FormEngine(sys.modules[__name__]).unconfirmed_checkbox_questions(scope)


def is_optional_follow_checkbox(cb: Locator) -> bool:
    label = cb.evaluate("e => [...(e.labels || [])].map(l => l.innerText || '').join(' ').trim()")
    recognized = (cb.get_attribute('id') == 'follow-company-checkbox'
                  or re.fullmatch(r'Follow .+ to stay up to date with their page', label if isinstance(label, str) else '', re.I) is not None
                  or re.fullmatch(r'Follow .+ to stay up to date with their page', question_of(cb).strip(), re.I) is not None)
    return recognized and not cb.evaluate("e => e.required || e.getAttribute('aria-required') === 'true'")


def clear_follow_preference(cb: Locator) -> bool:
    """LinkedIn hides the native input; its visible, non-submitting label drives it."""
    if not cb.is_checked():
        return True
    try:
        if cb.is_visible():
            cb.set_checked(False, timeout=1500)
        else:
            label = cb.locator('xpath=ancestor::label[1]')
            control_id = cb.get_attribute('id')
            if control_id:
                # Filter by DOM association, not CSS interpolation of an opaque ID.
                labels = cb.page.locator('label').all()
                matching = [item for item in labels if item.get_attribute('for') == control_id]
                if len(matching) == 1:
                    label = matching[0]
            if label.count() != 1:
                return False
            click_form_control(label)
        return not cb.is_checked()
    except Exception as e:
        if browser_closed(e):
            raise BrowserClosed() from None
        return False


def fill_checkgroups(scope: Locator) -> list[str]:
    return FormEngine(sys.modules[__name__]).fill_checkgroups(scope)


def standard_answer(el: Locator, q: str) -> str | None:
    return FormEngine(sys.modules[__name__]).standard_answer(el, q)


def is_phone_country_control(el: Locator, question: str) -> bool:
    return FormEngine(sys.modules[__name__]).is_phone_country_control(el, question)


def combobox_value_confirmed(el: Locator, question: str, candidates: list[str]) -> bool:
    return FormEngine(sys.modules[__name__]).combobox_value_confirmed(el, question, candidates)


from form_engine import TEXT_FIELDS


def text_field_candidates(el: Locator, q: str, *, external: bool) -> list[str]:
    return FormEngine(sys.modules[__name__]).text_field_candidates(el, q, external=external)


def text_value_confirmed(value: str, candidates: list[str], *, numeric: bool=False, question: str='') -> bool:
    return FormEngine(sys.modules[__name__]).text_value_confirmed(value, candidates, numeric=numeric, question=question)


def unconfirmed_text_questions(scope: Locator, *, external: bool) -> list[str]:
    return FormEngine(sys.modules[__name__]).unconfirmed_text_questions(scope, external=external)


from form_engine import FORM_VALUES_JS


def fill_step(page: Page, modal: Locator, external: bool=False) -> list[str]:
    return FormEngine(sys.modules[__name__]).fill_step(page, modal, external)


def _fill_step(page: Page, modal: Locator, external: bool=False) -> list[str]:
    return FormEngine(sys.modules[__name__])._fill_step(page, modal, external)


MODAL = "dialog[open], div[role='dialog']"
DISCARD_RE = re.compile(r"^(discard|sil|vazgeç)$", re.I)


def has_errors(modal: Locator) -> bool:
    return FormEngine(sys.modules[__name__]).has_errors(modal)


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
    return LinkedInAdapter(sys.modules[__name__]).linkedin_submission_signals(page)


def wait_linkedin_submission(page: Page, modal: Locator, before: set[str], timeout_seconds: float=25, expected_url: str | None=None) -> str:
    return LinkedInAdapter(sys.modules[__name__]).wait_linkedin_submission(page, modal, before, timeout_seconds, expected_url)


def submit_linkedin(page: Page, modal: Locator, primary: Locator, job_id: str, title: str, company: str, applied: dict | None=None) -> str:
    return LinkedInAdapter(sys.modules[__name__]).submit_linkedin(page, modal, primary, job_id, title, company, applied)


def submit_application(page: Page, primary: Locator, job_id: str, title: str, company: str,
                       url: str, wait_result, applied: dict | None = None) -> str:
    """Durably track every intentional final submit, even on timeout/STOP/crash."""
    if STOP.is_set():
        raise Stopped
    identity = application_identity.application_key(url, sys.modules[__name__])
    if identity:
        existing = applied if applied is not None else load_applied()
        blocker = application_identity.ledger(sys.modules[__name__]).submit_blocker(identity, job_id, existing, sys.modules[__name__])
        if blocker:
            return blocker
        application_identity.ledger(sys.modules[__name__]).bindings[job_id] = {'application_key': identity, 'application_url': url}
    if DRY_RUN:
        return preview_application(job_id)  # Defense in depth, including the preview budget.
    state = applied if applied is not None else load_applied()
    previous = state.get(job_id)
    if previous and not can_retry(previous["status"]) and not deferred_review.authorized(job_id, previous):
        if previous["status"].startswith("applied"):
            return "applied_previously"
        if previous["status"] in UNCERTAIN_STATUSES:
            record_manual(job_id, title, company, url, "previous uncertain attempt: check application history before retrying")
            return "submission_unconfirmed"
        return previous["status"]
    source = source_for_job(job_id)
    manual = deferred_review.CURRENT.get()
    if manual is not None:
        if manual.job_id != job_id:
            return 'manual_review'
        scope = primary.locator('xpath=ancestor::*[self::form or self::dialog or @role="dialog"][1]')
        if scope.count() != 1 or not manual.wait(sys.modules[__name__], page, scope,
                                                ['Final review: confirm this application before sending']):
            return 'manual_review'
        ats = ats_provider(page.url)
        if ats:
            check_ats_form(page, page.locator('html[data-autoapply-document]'), scope, ats)
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
        if manual is not None:
            page.evaluate('''() => { window.__autoapplyManualClickUsed = false;
                window.__autoapplyManualSubmitUsed = false; window.__autoapplyManualAllowSubmit = true; }''')
        primary.click()
        result = wait_result()
        if result in ("applied", "applied_external", "submission_rejected"):
            status = result
    except Exception as e:
        if browser_closed(e):
            raise BrowserClosed() from None
        log(f"  ! submission outcome unknown: {e.__class__.__name__}")
    finally:
        if manual is not None:
            try:
                page.evaluate('window.__autoapplyManualAllowSubmit = false')
            except Exception:
                pass
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
    return ATSEngine(sys.modules[__name__]).application_submission_signals(page)


def wait_application_submission(page: Page, before: set[str], checks: int=25, expected_host: str | None=None) -> str:
    return ATSEngine(sys.modules[__name__]).wait_application_submission(page, before, checks, expected_host)


def apply_to_job(page: Page, job_id: str, title: str='', company: str='', applied: dict | None=None) -> str:
    return LinkedInAdapter(sys.modules[__name__]).apply_to_job(page, job_id, title, company, applied)


def linkedin_navigation_issues(form: Locator) -> list[str]:
    return LinkedInAdapter(sys.modules[__name__]).linkedin_navigation_issues(form)


# ---------------------------------------------------------------- external (company sites)

ATS = {
    "greenhouse": frozenset({"boards.greenhouse.io", "boards.eu.greenhouse.io",
                             "job-boards.greenhouse.io", "job-boards.eu.greenhouse.io"}),
    "lever": frozenset({"jobs.lever.co", "jobs.eu.lever.co"}),
    "ashby": frozenset({"jobs.ashbyhq.com"}),
}


class UntrustedApplicationSite(Exception):
    """The application left its approved ATS document or has an unsafe target."""


class ApplicationFormChanged(UntrustedApplicationSite):
    """A field/form was replaced while filling; never reuse positional locators."""


def ensure_application_scope(scope: Locator, field: Locator | None=None, *, question: str='') -> None:
    return ATSEngine(sys.modules[__name__]).ensure_application_scope(scope, field, question=question)


def stable_application_controls(scope: Locator, selector: str):
    return ATSEngine(sys.modules[__name__]).stable_application_controls(scope, selector)


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
    return ATSEngine(sys.modules[__name__]).trusted_ats_document(page, ats)


def check_ats_form(page: Page, document: Locator, form: Locator, ats: str) -> None:
    return ATSEngine(sys.modules[__name__]).check_ats_form(page, document, form, ats)


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
    manual = deferred_review.CURRENT.get()
    instructions = getattr(manual, 'description_instructions', []) if manual is not None and manual.job_id == job_id else []
    issues = [*instructions, *unanswered]
    try:
        issues += unconfirmed_text_questions(form, external=external)
        issues += unconfirmed_radio_questions(form)
        issues += unconfirmed_checkbox_questions(form)
        for sel in form.locator('select:visible').all():
            if sel.is_disabled():
                continue
            q = question_of(sel)
            idx = choose_option(q, sel.locator('option').all_inner_texts())
            if idx is None or sel.evaluate('e => e.selectedIndex') != idx:
                issues.append(q or 'unconfirmed select')
        issues += native_form_issues(form)
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
    manual = deferred_review.CURRENT.get()
    if manual is not None and manual.job_id == job_id and 'form inspection failed' not in issues:
        if manual.wait(sys.modules[__name__], page, form, issues):
            return False
    return True


def native_form_issues(form: Locator) -> list[str]:
    return FormEngine(sys.modules[__name__]).native_form_issues(form)


def external_url(page: Page) -> str | None:
    return LinkedInAdapter(sys.modules[__name__]).external_url(page)


def follow_apply(page: Page, btn: Locator) -> str | None:
    return ATSEngine(sys.modules[__name__]).follow_apply(page, btn)


def ats_apply_url(page: Page, url: str) -> tuple[str | None, str]:
    return ATSEngine(sys.modules[__name__]).ats_apply_url(page, url)


EXT_STATE = {"count": 0, "last": 0.0}


def apply_external(page: Page, job_id: str, title: str, company: str, applied: dict | None = None) -> str:
    url = external_url(page)
    if not url:
        return "no_apply_button"
    return apply_form(page, job_id, title, company, url, applied)


def apply_form(page: Page, job_id: str, title: str, company: str, url: str, applied: dict | None=None) -> str:
    return ATSEngine(sys.modules[__name__]).apply_form(page, job_id, title, company, url, applied)


def apply_trusted_form(page: Page, job_id: str, title: str, company: str, form_url: str, ats: str, applied: dict | None=None) -> str:
    return ATSEngine(sys.modules[__name__]).apply_trusted_form(page, job_id, title, company, form_url, ats, applied)


# ---------------------------------------------------------------- search

def search_url(keyword: str, location: str) -> str:
    return LinkedInAdapter(sys.modules[__name__]).search_url(keyword, location)


SCRAPE_IDS_JS = 'async () => (await (' + linkedin_search.COLLECT_JS + ')()).ids'


def open_list(page: Page, url: str) -> list[str] | None:
    return LinkedInAdapter(sys.modules[__name__]).open_list(page, url)


def job_lists(page: Page):
    return LinkedInAdapter(sys.modules[__name__]).job_lists(page)


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
    return LinkedInAdapter(sys.modules[__name__]).job_info(page)


def job_ok(title: str, company: str, description: str) -> str | None:
    """Return None for an eligible job, otherwise the rejection reason."""
    f = CFG["filters"]
    t, d = title.lower(), description.lower()
    if not title.strip() or not company.strip():
        return 'identity_unknown'
    job_relevance.validate_settings(SETTINGS)
    if job_relevance.excluded_title_word(title, SETTINGS):
        return 'title_keyword_excluded'
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
    reason = job_relevance.reason(title, description, **technology_filters())
    # Targets are advisory: missing stack evidence must not block processing.
    # Explicit user exclusions still reject the vacancy normally.
    return None if reason == 'stack_unknown' else reason


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
        return check_description_instructions(job, applied)
    unknown = reason in ("posting_date_unknown", "posting_date_future", "workplace_unknown", 'location_unknown')
    status = "metadata_manual" if unknown else reason
    log(f"- skip [{reason}] {job['title']} @ {job['company']}")
    if unknown:
        diagnostic = (job.get('metadata') or {}).get('diagnostic')
        record_manual(job["id"], job["title"], job["company"], job["url"], reason + (': ' + json.dumps(diagnostic, ensure_ascii=False) if diagnostic else ''))
    METRICS.mark('manual' if unknown else 'rejected', job['id'])
    record(applied, job["id"], job["title"], job["company"], status, job["url"])
    if applied.get(job['id'], {}).get('decisions'):
        applied[job['id']]['reason'] = reason
        applied[job['id']]['metadata_diagnostic'] = (job.get('metadata') or {}).get('diagnostic')
        applied[job['id']]['decisions'][-1].update(reason=reason, metadata_diagnostic=applied[job['id']]['metadata_diagnostic'])
        write_applied(applied)
    return False


def check_description_instructions(job: dict, applied: dict) -> bool:
    """Defer writing/attention checks before opening a form; never invent answers."""
    excerpts = description_instructions.find(job.get('desc') or (job.get('metadata') or {}).get('description') or '')
    if not excerpts:
        return True
    issues = ['Description instruction — review the required wording and destination: ' + excerpt for excerpt in excerpts]
    manual = deferred_review.CURRENT.get()
    if manual is not None and manual.job_id == job['id']:
        manual.description_instructions = issues
        return True  # Each form step must still receive explicit human approval.
    previous = applied.get(job['id'], {})
    if previous.get('status', '').startswith('applied') or previous.get('status') in UNCERTAIN_STATUSES | {'submission_rejected'}:
        return False
    reason = '\n'.join(issues)
    log(f"  ? description instructions: manual review required for {job['title']} @ {job['company']}")
    record_manual(job['id'], job['title'], job['company'], job['url'], reason)
    record(applied, job['id'], job['title'], job['company'], 'manual_review', job['url'])
    applied[job['id']].update(reason=reason, description_instructions=excerpts)
    write_applied(applied)
    METRICS.mark('manual', job['id'])
    return False


RECHECK_STATE = {'id': secrets.token_hex(8), 'count': 0}
MANUAL_RELEVANCE = frozenset({'identity_unknown', 'stack_unknown'})


def begin_job_rechecks(applied: dict) -> None:
    RECHECK_STATE.update(id=secrets.token_hex(8), count=0)
    if SETTINGS.get('recheck_enabled', CFG.get('rechecks', {}).get('enabled', False)):
        if job_rechecks.prepare(applied, [APPLIED_FILE, LOG_FILE, MANUAL_FILE], HOME / 'data' / 'backups'):
            write_applied(applied)


def should_visit_job(applied: dict, job_id: str) -> bool:
    METRICS.mark('seen', job_id)
    previous = applied.get(job_id)
    if previous is None or can_retry(previous['status']):
        return True
    if not SETTINGS.get('recheck_enabled', CFG.get('rechecks', {}).get('enabled', False)):
        METRICS.mark('history_skipped', job_id)
        return False
    options = CFG.get('rechecks', {})
    if job_rechecks.claim(previous, job_rechecks.revision(CFG, SETTINGS), RECHECK_STATE,
                          max_per_run=options.get('max_per_run', 10), max_per_job=options.get('max_per_job', 3),
                          cooldown_hours=options.get('cooldown_hours', 24)):
        write_applied(applied)
        return True
    METRICS.mark('history_skipped', job_id)
    return False


def record_relevance_decision(applied: dict, job_id: str, title: str, company: str, reason: str, url: str, description: str = '') -> None:
    METRICS.mark('manual' if reason in MANUAL_RELEVANCE else 'rejected', job_id)
    if reason in MANUAL_RELEVANCE:
        record_manual(job_id, title, company, url, reason)
    record(applied, job_id, title, company, 'relevance_manual' if reason in MANUAL_RELEVANCE else reason, url)
    if applied.get(job_id, {}).get('decisions'):
        applied[job_id]['reason'] = reason
        applied[job_id]['decisions'][-1]['reason'] = reason
        if reason == 'title_keyword_excluded':
            diagnostic = {'term': job_relevance.excluded_title_word(title, SETTINGS), 'evidence': title, 'source': 'title'}
            applied[job_id]['title_diagnostic'] = diagnostic
            applied[job_id]['decisions'][-1]['title_diagnostic'] = diagnostic
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
    from phone_contact import split_phone
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
        code, number = split_phone(prof['phone_intl'])
        PROFILE['phone'] = number
        PROFILE['country_code'] = f'({code})' if code else ''
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
        METRICS.mark('seen', job_id)
        previous = applied.get(job_id)
        if previous and not can_retry(previous['status']):
            authorized_recheck = (previous['status'] in job_rechecks.RECHECKABLE
                                 and previous.get('recheck', {}).get('run_id') == RECHECK_STATE['id'])
            if not authorized_recheck and not deferred_review.authorized(job_id, previous):
                METRICS.mark('history_skipped', job_id)
                return False
        if submission_quota_status('linkedin'):
            return False
        page.goto(f"https://www.linkedin.com/jobs/view/{job_id}/", wait_until="domcontentloaded")
        pause(2.5, 4.5)
        title, company, desc = job_info(page)
        METRICS.mark('evaluated', job_id)
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
        with METRICS.application(job_id):
            status = apply_to_job(page, job_id, title, company, applied)
            if status == "no_easy_apply" and EXTERNAL:
                status = apply_external(page, job_id, title, company, applied)
        METRICS.outcome(job_id, status)
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
        METRICS.mark('errors', job_id)
        screenshot(page, f"{job_id}_error")
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        return False


def linkedin_login(page: Page) -> bool:
    return LinkedInAdapter(sys.modules[__name__]).linkedin_login(page)


def run_linkedin(page: Page, applied: dict, count: int) -> int:
    return LinkedInAdapter(sys.modules[__name__]).run_linkedin(page, applied, count)


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
    METRICS.reset()
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
    log('Finished. ' + METRICS.summary(submit_attempts=0 if DRY_RUN else ATTEMPTS['total']))
    pending = deferred_review.queue(applied, MANUAL_FILE, sys.modules[__name__])
    log(f"Deferred applications awaiting manual input: {sum(job['category'] == 'manual' for job in pending)}; not processed due to limits: {sum(job['category'] == 'limit' for job in pending)}. Open Deferred applications in the GUI after the run.")
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
