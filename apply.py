#!/usr/bin/env python3
"""LinkedIn Easy Apply botu.

Son 24 saatteki AI ilanlarını tarar, config.yaml'daki filtrelere uyanlara
Easy Apply ile başvurur. Başvurulan ilanlar data/applied.json'da tutulur,
böylece aynı ilana iki kez başvurulmaz.

Oturum .browser-profile/ içinde saklanır; ilk çalıştırmada açılan Chrome
penceresinde LinkedIn'e bir kez giriş yapılır.

Başlarken terminalden CV dosyasını, maaş beklentisini, İngilizce seviyesini ve
ihbar süresini sorar (Enter = önceki cevap). CV Claude API ile okunur; çıkan
pozisyonlar ve bilgiler kullanıcı onayından sonra arama ve formlarda kullanılır.
Cevaplar data/user_settings.json'da saklanır. Sadece Kolay Başvuru (Easy Apply) ilanlarına başvurur.

Ortam değişkenleri:
  DRY_RUN   "true" ise formları doldurur ama son adımda göndermez
  HEADLESS  "true" ise pencere açmadan çalışır
"""
import csv
import datetime as dt
import hashlib
import json
import os
import random
import re
import secrets
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlencode, urlparse

import yaml
import job_metadata
from playwright.sync_api import Locator, Page, sync_playwright
from playwright.sync_api import TimeoutError as PWTimeout

ROOT = Path(__file__).resolve().parent  # kod ve varsayılan config.yaml
# Kullanıcı verisi: kaynak koddan çalışırken proje klasörü, indirilen uygulamada ~/AutoApply
HOME = Path(os.getenv("AUTOAPPLY_HOME") or (Path.home() / "AutoApply" if getattr(sys, "frozen", False) else ROOT))
(HOME / "data").mkdir(parents=True, exist_ok=True)
CFG = yaml.safe_load(((HOME if (HOME / "config.yaml").exists() else ROOT) / "config.yaml").read_text(encoding="utf-8"))
APPLIED_FILE = HOME / "data" / "applied.json"
LOG_FILE = HOME / "data" / "applications.csv"
LAST_RUN_FILE = HOME / "data" / "last_run.txt"
MANUAL_FILE = HOME / "data" / "manual_apply.csv"
SHOTS = HOME / "screenshots"
PROFILE_DIR = HOME / ".browser-profile"
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
DEADLINE = time.time() + CFG["limits"]["max_runtime_minutes"] * 60
STOP = threading.Event()  # arayüzdeki "Durdur" butonu
PROFILE = CFG["profile"]
EXTERNAL = CFG.get("external", {}).get("enabled", False)
SETTINGS_FILE = HOME / "data" / "user_settings.json"
SETTINGS: dict = {}

# İngilizce seviyesi -> formlardaki seçeneklerde sırayla denenecek cevaplar
ENGLISH_LEVELS = {
    "1": ("Native / iki dilli", ["Native or bilingual", "Native", "Bilingual", "Fluent", "C2"]),
    "2": ("Akıcı / profesyonel", ["Full professional", "Professional working", "Fluent", "Advanced", "C1"]),
    "3": ("Orta / konuşma", ["Conversational", "Intermediate", "Limited working", "B2", "Komunikatywny", "Średnio zaawansowany"]),
    "4": ("Temel", ["Elementary", "Basic", "Beginner", "A2"]),
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
  // Yeni LinkedIn formu: soru metni alana bağlı değil; yukarı çıkıp ilk anlamlı satırı al.
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


# Bu durumlarla kaydedilen ilanlar sonraki çalıştırmada tekrar denenir
RETRY_PREFIXES = ("skipped_", "modal_not_opened", "blocked") + (("no_easy_apply",) if EXTERNAL else ())


class LimitReached(Exception):
    """Başvuru limiti veya süre doldu."""


class Stopped(BaseException):
    """Kullanıcı durdurdu (BaseException: kaynaklardaki 'except Exception' bloklarında yutulmasın)."""


class BrowserClosed(Stopped):
    """Kullanıcı Chrome penceresini kapattı."""


def browser_closed(e: BaseException) -> bool:
    return bool(re.search(r"has been closed|Target closed|Browser closed|Connection closed", str(e)))


class SessionLost(Exception):
    """LinkedIn oturumu sonlandırdı (çerez geçersiz)."""


LOG_LINES: list[str] = []  # arayüz buradan okur


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
        pass  # pencereli uygulamada konsol yok


def pause(a: float = 1.5, b: float = 3.5) -> None:
    if STOP.wait(random.uniform(a, b)):
        raise Stopped


# ---------------------------------------------------------------- state

def load_applied() -> dict:
    try:
        return json.loads(APPLIED_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def job_key(title: str, company: str) -> str:
    """Aynı ilan farklı sitelerde çıkınca tanımak için şirket+başlık anahtarı."""
    return re.sub(r"\W+", "", f"{company}|{title}".lower())


def applied_elsewhere(applied: dict, title: str, company: str) -> bool:
    key = job_key(title, company)
    return any(v.get("key") == key and (v["status"].startswith("applied")
               or v["status"] in ("submission_pending", "submission_unconfirmed")) for v in applied.values())


def record(applied: dict, job_id: str, title: str, company: str, status: str, url: str = "") -> None:
    applied[job_id] = {"status": status, "date": dt.date.today().isoformat(), "key": job_key(title, company)}
    temporary = APPLIED_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(applied, indent=1, ensure_ascii=False))
    temporary.replace(APPLIED_FILE)
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
    """check(page) True olana kadar kullanıcının pencerede doğrulama/giriş yapmasını bekler."""
    if check(page):
        return True
    if os.getenv("HEADLESS", "false").lower() == "true":
        return False
    log(f"  !! Açık Chrome penceresinde {what} (en fazla {minutes} dk bekleniyor)...")
    end = time.time() + minutes * 60
    while time.time() < end:
        pause(3, 3)
        if check(page):
            pause(2, 3)
            return True
    log(f"  !! {what} yapılmadı, bu kaynak atlanıyor.")
    return False


def screenshot(page: Page, name: str) -> None:
    SHOTS.mkdir(exist_ok=True)
    try:
        page.screenshot(path=str(SHOTS / f"{name}.png"), full_page="_ext_" in name)
    except Exception:
        pass


# ---------------------------------------------------------------- answers

def answer(question: str, numeric: bool = False) -> list[str]:
    """Soruya verilecek aday cevaplar (ilk eşleşen kural)."""
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
    """1 USD = ? birim. Ücretsiz kaynaklar; ulaşılamazsa boş (dönüşüm yapılmaz)."""
    if not RATES:
        import urllib.request
        from cv_profile import SSL_CTX
        for url in ("https://api.frankfurter.dev/v1/latest?base=USD", "https://open.er-api.com/v6/latest/USD"):
            try:
                with urllib.request.urlopen(url, timeout=15, context=SSL_CTX) as r:
                    RATES.update(json.loads(r.read())["rates"], USD=1.0)
                if "TRY" in RATES:
                    break
            except Exception:
                continue
    return RATES


def salary_answer(question: str) -> str:
    """Yıllık beklentiyi sorudaki para birimine ve döneme (aylık/saatlik/bin) çevirir."""
    q = question.lower()
    turkish = bool(re.search(r"maaş|ücret", q))
    amount = float(SETTINGS.get("salary") or 0)
    have = SETTINGS.get("currency", "USD")
    # Dolar işareti CAD sorularında da geçer; önce özel kodlara bak
    want = next((c for c in ("CAD", "TRY", "PLN", "CHF", "INR", "GBP", "EUR", "USD") if re.search(CURRENCY_RE[c], q)),
                "TRY" if turkish else have)  # Türkçe soru, birim yok: TL
    fixed = SETTINGS.get("monthly_salary_by_currency") or {}  # ör. {"EUR": 2200, "TRY": 80000}: kurla çevirmek yerine bu
    if want in fixed:
        amount = float(fixed[want]) * 12
    elif want != have:
        r = rates()
        if have in r and want in r:
            amount = amount / r[have] * r[want]
    if re.search(r"hour|saatlik|stündlich|\brate\b", q):
        amount, step = amount / 2080, 1
    elif re.search(r"\bday\b|daily|günlük", q):
        amount, step = amount / 220, 5
    elif re.search(r"month|aylık|monat|mensuel", q) or ((want == "TRY" or turkish) and not re.search(r"year|annual|yıllık", q)):
        # Türkiye'de dönem yazmayan maaş sorusu aylık demektir
        amount, step = amount / 12, 100
    else:
        step = 1000
    if re.search(r"in thousands|\(k\)|thousands of|bin (tl|usd|eur)", q):
        amount, step = amount / 1000, 1
    return str(int(max(step, round(amount / step) * step)))


def placeholder(a: str) -> str:
    """"__SALARY__" gibi yer tutucuları başlangıç cevaplarından / profilden doldurur."""
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
    """Yalnızca doğrulanmış yıl sayısını içeren bir aralık seç; en yakınını tahmin etme."""
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
    """Seçenekler arasından cevabın index'ini döndür."""
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

RADIO_GROUP_JS = r"""fs => {
  const t = s => (s || '').replace(/\s+/g, ' ').trim();
  const lines = el => (el.innerText || '').split('\n').map(t).filter(Boolean);
  const roles = [...fs.querySelectorAll('[role=radio]')];
  const labels = roles.length
    ? roles.map(r => lines(r).pop() || '')
    : [...fs.querySelectorAll('input[type=radio]')].map(r => {
        const l = r.id && document.querySelector(`label[for="${CSS.escape(r.id)}"]`);
        if (l && t(l.innerText)) return t(l.innerText);
        // Etiket boşsa metni olan en yakın üst öğe (seçenek satırı)
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


def fill_radios(scope: Locator) -> list[str]:
    """Radyo butonlu sorular; cevaplanamayanları döndürür. Yeni LinkedIn formunda
    seçenekler div[role=radio] ve <label> boş; eski formda <legend> + <label for>."""
    unanswered: list[str] = []
    # Preselected options also need a confirmed matching answer.
    for fs in scope.locator("fieldset:has(input[type='radio']):visible, [role='radiogroup']:visible").all():
        try:
            q, labels = fs.evaluate(RADIO_GROUP_JS)
            idx = choose_option(q, labels)
            if idx is None:
                log(f"    ? cevapsız soru: {q[:90]}")
                unanswered.append(q)
                continue
            items = fs.locator("[role='radio']")
            if items.count():
                items.nth(idx).click()
            else:
                radio = fs.locator("input[type='radio']").nth(idx)
                # Metni olan en yakın üst öğe = tıklanabilir seçenek satırı
                row = radio.locator("xpath=ancestor::*[normalize-space(.)!=''][1]")
                try:
                    row.click(timeout=5000)
                except PWTimeout:
                    radio.check(force=True)
                if not fs.locator("input[type='radio']:checked").count():
                    radio.check(force=True)
            pause(0.3, 0.7)
        except Exception as e:
            if browser_closed(e):
                raise BrowserClosed() from None
            log(f"    ! radio doldurulamadı: {e.__class__.__name__}")
            unanswered.append(question_of(fs) or "radio group")
    return unanswered


# Dış sitelerdeki standart alanlar: (soru/name/id regex'i, profile anahtarı). İlk eşleşen kazanır.
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
    """Greenhouse/Ashby'deki aranabilir açılır listeler (role=combobox)."""
    std = standard_answer(el, q)
    cands = [std] if std else answer(q)
    if not cands:
        return False
    try:
        el.click()
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
        opts.nth(idx).click()
        pause(0.3, 0.7)
        return True
    except Exception as e:
        if browser_closed(e):
            raise BrowserClosed() from None
        log(f"    ! liste doldurulamadı: {e.__class__.__name__}")
        return False


YESNO_JS = r"""root => {
  // Yes/No düğme çiftleri (Ashby). Her çift için [soru, seçili mi] döndürür; düğmeleri işaretler.
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
    const picked = btns.some(x => /active|selected/i.test(x.className) || x.getAttribute('aria-pressed') === 'true');
    box.setAttribute('data-yesno', out.length);
    out.push([q, picked]);
  }
  return out;
}"""

CHECKGROUP_JS = r"""root => {
  // Çoklu seçim soruları: aynı kapta 2+ onay kutusu. [soru, seçenekler, herhangi biri seçili mi]
  const t = s => (s || '').replace(/\s+/g, ' ').trim();
  const label = c => {
    const l = c.id && document.querySelector(`label[for="${CSS.escape(c.id)}"]`);
    if (l && t(l.innerText)) return t(l.innerText);
    let p = c.parentElement;
    while (p && !t(p.innerText)) p = p.parentElement;
    return p ? t(p.innerText) : '';
  };
  const groups = new Map();
  for (const c of root.querySelectorAll('input[type=checkbox]')) {
    let g = c.parentElement;
    while (g && g !== root && g.querySelectorAll('input[type=checkbox]').length < 2) g = g.parentElement;
    if (!g || g === root) continue;
    if (!groups.has(g)) groups.set(g, []);
    groups.get(g).push(c);
  }
  const out = [];
  for (const [g, cbs] of groups) {
    const opts = cbs.map(label);
    let p = g, q = '';
    for (let i = 0; i < 4 && p && !q; i++, p = p.parentElement)
      q = (p.innerText || '').split('\n').map(t).find(l => l && !opts.includes(l)) || '';
    g.setAttribute('data-checkgroup', out.length);
    out.push([q, opts, cbs.some(c => c.checked)]);
  }
  return out;
}"""


def fill_yesno(scope: Locator) -> list[str]:
    unanswered = []
    for i, (q, _picked) in enumerate(scope.evaluate(YESNO_JS)):
        idx = choose_option(q, ["Yes", "No"])
        if idx is None:
            unanswered.append(q or "Yes/No question")
            continue
        try:
            scope.locator(f"[data-yesno='{i}'] button").nth(idx).click()
            pause(0.3, 0.6)
        except Exception:
            unanswered.append(q)
    return unanswered


def fill_checkgroups(scope: Locator) -> list[str]:
    """Select only explicitly configured options; unknown groups need manual review."""
    unanswered = []
    for i, (q, opts, _any_checked) in enumerate(scope.evaluate(CHECKGROUP_JS)):
        cands = answer(q)
        pick = [j for c in cands if (j := match_option([c], opts)) is not None]
        if not pick:
            unanswered.append(q or "checkbox group")
            continue
        cbs = scope.locator(f"[data-checkgroup='{i}'] input[type='checkbox']")
        for j in range(len(opts)):
            try:
                cb = cbs.nth(j)
                wanted = j in pick
                if cb.is_checked() == wanted:
                    continue
                row = cb.locator("xpath=ancestor::*[normalize-space(.)!=''][1]")
                try:
                    row.click(timeout=4000)
                except PWTimeout:
                    cb.set_checked(wanted, force=True)
                if cb.is_checked() != wanted:
                    cb.set_checked(wanted, force=True)
            except Exception as e:
                log(f"    ! onay kutusu seçilemedi: {e.__class__.__name__}")
                unanswered.append(q or "checkbox group")
    return unanswered


def standard_answer(el: Locator, q: str) -> str | None:
    """Dış sitelerdeki ad/e-posta/telefon gibi standart alanlar."""
    if SENSITIVE_RE.search(q) or NARRATIVE_RE.search(q):
        return None
    key = " ".join([q, el.get_attribute("name") or "", el.get_attribute("id") or "",
                    el.get_attribute("autocomplete") or ""]).lower()
    for pattern, field in STANDARD_FIELDS:
        if re.search(pattern, key):
            return PROFILE.get(field) or None
    return None


def fill_step(page: Page, modal: Locator, external: bool = False) -> list[str]:
    """Formdaki boş alanları doldurur; cevaplanamayan soruları döndürür."""
    unanswered: list[str] = []
    # Metin alanları
    fields = modal.locator(
        "input[type='text']:visible, input[type='tel']:visible, input[type='email']:visible, "
        "input[type='number']:visible, input[type='url']:visible, input[type='date']:visible, "
        "input:not([type]):visible, textarea:visible"
    )
    for el in fields.all():
        try:
            if not el.is_editable(timeout=2000):
                continue
            invalid = el.get_attribute("aria-invalid") == "true"
            if el.input_value().strip() and not invalid:
                continue
            q = question_of(el)
            if external and el.get_attribute("role") == "combobox":
                if not fill_combobox(page, el, q):
                    unanswered.append(q)
                continue
            std = standard_answer(el, q) if external else None
            if std:
                el.fill(std)
                pause(0.3, 0.8)
                continue
            numeric = el.get_attribute("type") == "number" or "numeric" in (el.get_attribute("id") or "")
            # Dolu ama geçersiz alan genelde sayı bekliyor
            cands = answer(q, numeric=numeric or invalid)
            if not cands:
                log(f"    ? cevapsız soru: {q[:90]}")
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
                    sugg.nth(idx).click()
        except Exception as e:
            if browser_closed(e):
                raise BrowserClosed() from None
            try:
                q = question_of(el)[:70]
            except Exception:
                q = "?"
            log(f"    ! alan doldurulamadı, atlandı ({e.__class__.__name__}): {q}")
            unanswered.append(q or "text field")

    # Açılır listeler
    for sel in modal.locator("select:visible").all():
        try:
            options = sel.locator("option").all_inner_texts()
            idx = choose_option(question_of(sel), options)
            if idx is not None:
                sel.select_option(index=idx)
            else:
                unanswered.append(question_of(sel) or "select")
        except Exception as e:
            log(f"    ! select doldurulamadı: {e.__class__.__name__}")
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
        # Dış sitelerde CV her zaman yüklenir; ön yazı dosyası alanları atlanır
        for fi in modal.locator("input[type='file']").all():
            key = (question_of(fi) + " " + (fi.get_attribute("name") or "") + " " + (fi.get_attribute("id") or "")).lower()
            if re.search(r"cover", key) or not resume.exists():
                continue
            if re.search(r"resume|\bcv\b|lebenslauf|özgeçmiş", key) or modal.locator("input[type='file']").count() == 1:
                try:
                    fi.set_input_files(str(resume))
                    pause(2, 3)
                except Exception as e:
                    log(f"    ! CV yüklenemedi: {e.__class__.__name__}")
                    unanswered.append("resume upload")
        return unanswered

    # CV: başta seçilen dosya LinkedIn'de listede yoksa yükle (yüklenen otomatik seçilir)
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
            btn.first.click(timeout=4000)
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
    state = applied if applied is not None else load_applied()
    url = f"https://www.linkedin.com/jobs/view/{job_id}/"
    # Persist before clicking: even a process crash must not cause a blind retry.
    record(state, job_id, title, company, "submission_pending", url)
    status = "submission_unconfirmed"
    try:
        if STOP.is_set():
            raise Stopped
        primary.click()
        status = wait_linkedin_submission(page, modal, before, expected_url=expected_url)
    except Exception as e:
        if browser_closed(e):
            raise BrowserClosed() from None
        log(f"  ! submission outcome unknown: {e.__class__.__name__}")
    finally:
        # Also runs for STOP/BrowserClosed (BaseException), preserving uncertainty.
        record(state, job_id, title, company, status, url)
        if status != "applied":
            screenshot(page, f"{job_id}_{status}")
            record_manual(job_id, title, company, url, status + ": check LinkedIn application history before retrying")
    if status == "applied":
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass  # A cleanup failure must not erase confirmed success.
    return status


def apply_to_job(page: Page, job_id: str, title: str = "", company: str = "", applied: dict | None = None) -> str:
    btn = page.get_by_role("button", name=EASY_APPLY_RE).first
    if not btn.count():
        return "no_easy_apply"
    btn.click()
    modal = page.locator(MODAL).first
    try:
        modal.wait_for(timeout=10000)
        modal.locator("footer button").first.wait_for(timeout=20000)  # form yüklenirken spinner döner
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
            modal.locator("label[for='follow-company-checkbox']").click()

        primary = modal.locator("footer button:visible").last
        if not primary.count():
            screenshot(page, f"{job_id}_nobutton")
            discard(page)
            return "skipped_no_button"
        text = (primary.inner_text() + " " + (primary.get_attribute("aria-label") or "")).lower()

        if "submit" in text or "gönder" in text:
            if DRY_RUN:
                screenshot(page, f"{job_id}_dryrun")
                discard(page)
                return "dry_run"
            return submit_linkedin(page, modal, primary, job_id, title, company, applied)

        before = modal.inner_text()
        primary.click()
        pause(1.5, 3)
        if has_errors(modal) or modal.inner_text() == before:
            unanswered = fill_step(page, modal)  # bir kez daha dene
            if require_manual_review(page, modal, unanswered, job_id, title, company):
                discard(page)
                return "manual_review"
            primary.click()
            pause(1.5, 3)
            if has_errors(modal) or modal.inner_text() == before:
                screenshot(page, f"{job_id}_unanswered")
                discard(page)
                return "skipped_unanswered"

    screenshot(page, f"{job_id}_toolong")
    discard(page)
    return "skipped_too_many_steps"


# ---------------------------------------------------------------- external (şirket sitesi)

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
DONE_RE = re.compile(
    r"thank(s| you)|application (was |has been )?(received|submitted|sent)|we.ve received|"
    r"successfully (submitted|applied)|teşekkür|başvurunuz (alındı|iletildi)", re.I)
CAPTCHA_SEL = "iframe[src*='hcaptcha'][title*='challenge'], iframe[src*='recaptcha'][title*='challenge']:visible"


def record_manual(job_id: str, title: str, company: str, url: str, reason: str) -> None:
    new_file = not MANUAL_FILE.exists()
    with MANUAL_FILE.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["date", "job_id", "title", "company", "reason", "apply_url"])
        w.writerow([dt.date.today().isoformat(), job_id, title, company, reason, url])


def require_manual_review(page: Page, form: Locator, unanswered: list[str], job_id: str,
                          title: str, company: str, url: str = "") -> bool:
    """Fail closed before advancing/submitting, including when form inspection fails.

    Unknown questions are conservative: even optional ones block unless already filled.
    Native validation also catches unsupported required fields and failed file uploads.
    """
    issues = list(unanswered)
    try:
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
    """LinkedIn'deki 'Başvur' butonuna basıp şirket sitesinin adresini döndürür."""
    btn = page.get_by_role("button", name=APPLY_BTN_RE).or_(page.get_by_role("link", name=APPLY_BTN_RE))
    return follow_apply(page, btn.filter(has_not_text=EASY_APPLY_RE).first)


def follow_apply(page: Page, btn: Locator) -> str | None:
    """Başvur butonuna basar; açılan sekmenin ya da gidilen sayfanın adresini döndürür."""
    if not btn.count():
        return None
    ctx = page.context
    before, start = len(ctx.pages), page.url
    btn.click()
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
        # "Profilini paylaş / Devam et" ara penceresi
        cont = page.locator(MODAL).get_by_role("button", name=re.compile(r"continue|devam", re.I))
        if cont.count():
            cont.first.click()
    return None


def ats_apply_url(page: Page, url: str) -> tuple[str | None, str]:
    """Başvuru formunun doğrudan adresi ve ATS adı. Şirket sitesine gömülü formları da bulur."""
    name = ats_provider(url)
    if not name:
        if not safe_https_url(url):
            return None, "unknown"
        # Şirket sitesine gömülü Greenhouse/Ashby/Lever iframe'i
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


def apply_external(page: Page, job_id: str, title: str, company: str) -> str:
    url = external_url(page)
    if not url:
        return "no_apply_button"
    return apply_form(page, job_id, title, company, url)


def apply_form(page: Page, job_id: str, title: str, company: str, url: str) -> str:
    """Şirket sitesindeki başvuru formu. Spam sayılmamak için çalıştırma başına sınırlı ve aralıklı."""
    ext = CFG["external"]
    if not EXTERNAL:
        record_manual(job_id, title, company, url, "external_disabled")
        return "external_manual"
    if EXT_STATE["count"] >= ext.get("max_per_run", 5):
        return "skipped_external_limit"  # sonraki çalıştırmada tekrar denenir
    form_url, ats = ats_apply_url(page, url)
    if not form_url:
        record_manual(job_id, title, company, url, "unsupported_site")
        return "external_manual"
    wait = EXT_STATE["last"] + ext.get("min_wait_seconds", 120) - time.time()
    if wait > 0:
        log(f"  .. şirket formu öncesi {int(wait)} sn bekleniyor")
        pause(wait, wait)
    EXT_STATE["count"] += 1
    EXT_STATE["last"] = time.time()
    log(f"  -> {ats}: {form_url[:90]}")
    page.goto(form_url, wait_until="domcontentloaded", timeout=30000)
    pause(3, 5)
    try:
        return apply_trusted_form(page, job_id, title, company, form_url, ats)
    except UntrustedApplicationSite as e:
        log(f"  ! untrusted application site: {e}")
        record_manual(job_id, title, company, url, str(e))
        return "external_manual"


def apply_trusted_form(page: Page, job_id: str, title: str, company: str, form_url: str, ats: str) -> str:
    document = trusted_ats_document(page, ats)
    if ats == "ashby":
        tab = document.get_by_role("tab", name=re.compile(r"application", re.I))
        if tab.count():
            tab.first.click()
            pause(1.5, 2.5)
    form = document.locator("form:has(input[type='file']), form:has(textarea), form:has(input[type='email'])").first
    if not form.count():
        form = document.locator("body")

    check_ats_form(page, document, form, ats)
    unanswered = fill_step(page, form, external=True)
    pause(1, 2)
    check_ats_form(page, document, form, ats)
    unanswered += fill_step(page, form, external=True)  # açılan koşullu sorular için ikinci tur
    check_ats_form(page, document, form, ats)
    if require_manual_review(page, form, unanswered, job_id, title, company, form_url):
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
        return "dry_run"

    check_ats_form(page, document, form, ats)
    submit.last.click()
    for _ in range(25):
        time.sleep(1)
        if page.locator("[aria-invalid='true']:visible, [class*='error' i]:visible:has-text('required')").count():
            screenshot(page, f"{job_id}_ext_invalid")
            record_manual(job_id, title, company, form_url, "form_errors_after_submit")
            return "external_unanswered"
        if DONE_RE.search(page.locator("body").inner_text()) or re.search(r"confirm|thank|success", page.url):
            return "applied_external"
        if page.locator(CAPTCHA_SEL).count():
            break
    screenshot(page, f"{job_id}_ext_unconfirmed")
    record_manual(job_id, title, company, form_url, "submitted_unconfirmed_or_captcha")
    return "external_unconfirmed"


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
        params["f_AL"] = "true"  # sadece Easy Apply
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
        log(f"  ! sayfa açılamadı: {str(e)[:100]}")
        return None
    pause(3, 5)
    return list(dict.fromkeys(f for f in page.evaluate(SCRAPE_IDS_JS) if f and f.isdigit()))


def job_lists(page: Page):
    """Sırayla ilan listeleri üretir: önce İş İlanları sekmesi ve önerilenler, sonra aramalar."""
    found = open_list(page, "https://www.linkedin.com/jobs/") or []
    log(f"  İş İlanları sekmesi: {len(found)} ilan")
    yield found
    for p in range(CFG["search"]["pages_per_search"]):
        found = open_list(page, f"https://www.linkedin.com/jobs/collections/recommended/?start={p * 24}")
        if not found:
            break
        log(f"  Önerilen ilanlar s{p + 1}: {len(found)} ilan")
        yield found
    for kw in CFG["search"]["keywords"]:
        for loc in CFG["search"]["locations"]:
            for p in range(CFG["search"]["pages_per_search"]):
                found = open_list(page, search_url(kw, loc) + f"&start={p * 25}")
                if found is None:
                    break
                log(f"  '{kw}' @ {loc} s{p + 1}: {len(found)} ilan")
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
    """Başlık, şirket, açıklama. Sekme başlığı 'Pozisyon | Şirket | LinkedIn' biçiminde."""
    parts = [p.strip() for p in page.title().split("|")]
    title = parts[0] if len(parts) >= 3 else first_text(page, [".job-details-jobs-unified-top-card__job-title", "h1"])
    company = parts[1] if len(parts) >= 3 else first_text(page, ["a[href*='/company/']"])
    return title, company, page.evaluate(DESC_JS)


def job_ok(title: str, company: str, description: str) -> str | None:
    """Uygunsa None, değilse sebebini döndür."""
    f = CFG["filters"]
    t, d = title.lower(), description.lower()
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
    return None


def read_job_metadata(page: Page, source: str, url: str) -> dict:
    try:
        return job_metadata.page_metadata(page, source, url)
    except Exception as e:
        if browser_closed(e):
            raise BrowserClosed() from None
        log(f"  ? job metadata unavailable: {e.__class__.__name__}")
        return {}


def job_location_ok(text: str) -> bool:
    """Apply the configured accepted-region policy consistently across sources."""
    if not text.strip():
        return True
    regions = CFG["search"].get("accepted_regions", "anywhere|worldwide|global")
    country = re.escape(PROFILE.get("country", ""))
    return bool(re.search(rf"{regions}|{country}" if country else regions, text, re.I))


def check_job_eligibility(job: dict, applied: dict) -> bool:
    """Shared final gate: search URLs and board names are not eligibility evidence."""
    search = CFG["search"]
    reason = job_metadata.eligibility_reason(job.get("metadata") or {}, search["posted_within_hours"],
                                             search.get("remote_only", False))
    if reason is None and not job_location_ok((job.get("metadata") or {}).get("location") or job.get("location", "")):
        reason = "location_restricted"
    if reason is None:
        return True
    unknown = reason in ("posting_date_unknown", "posting_date_future", "workplace_unknown")
    status = "metadata_manual" if unknown else reason
    log(f"- atla [{reason}] {job['title']} @ {job['company']}")
    if unknown:
        record_manual(job["id"], job["title"], job["company"], job["url"], reason)
    record(applied, job["id"], job["title"], job["company"], status, job["url"])
    return False


# ---------------------------------------------------------------- başlangıç soruları

def ask(prompt: str, default: str) -> str:
    raw = input(f"{prompt} [{default}]: " if default else f"{prompt}: ").strip()
    return raw or default


def ask_settings() -> None:
    """CV, maaş ve İngilizce bilgisini kullanıcıdan alır; cevaplar bir sonraki sefere varsayılan olur."""
    load_settings()
    if not sys.stdin.isatty():
        log("Terminal yok, kayıtlı ayarlar kullanılıyor.")
    else:
        print("\n=== Başvuru bilgileri (Enter = köşeli parantezdeki değer) ===")
        while True:
            raw = ask("CV dosyası (PDF/DOCX yolu, dosyayı terminale sürükleyebilirsin)", SETTINGS["resume"])
            path = Path(raw.strip("'\" ").replace("\\ ", " ")).expanduser()
            path = path if path.is_absolute() else HOME / path
            if path.is_file() and path.suffix.lower() in (".pdf", ".doc", ".docx"):
                SETTINGS["resume"] = str(path)
                break
            print(f"  ! Dosya bulunamadı veya PDF/DOCX değil: {path}")
        while (cur := ask("Maaş para birimi (USD/EUR/GBP/TRY)", SETTINGS.get("currency", "USD")).upper()) not in CURRENCY_RE:
            pass
        SETTINGS["currency"] = cur
        while True:
            salary = re.sub(r"[^\d]", "", ask(f"Yıllık maaş beklentisi ({cur}, sadece sayı)", SETTINGS["salary"]))
            if salary:
                SETTINGS["salary"] = salary
                break
        print("İngilizce seviyesi:")
        for key, (label, _) in ENGLISH_LEVELS.items():
            print(f"  {key}) {label}")
        while (eng := ask("Seçim", SETTINGS["english"])) not in ENGLISH_LEVELS:
            pass
        SETTINGS["english"] = eng
        SETTINGS["notice"] = re.sub(r"[^\d]", "", ask("İhbar süresi (gün)", SETTINGS["notice"])) or SETTINGS["notice"]
        cv_setup()
        print()
    finalize_settings()


def load_settings() -> dict:
    saved = json.loads(SETTINGS_FILE.read_text()) if SETTINGS_FILE.exists() else {}
    SETTINGS.update({"resume": PROFILE["resume_file"], "salary": "75000", "currency": "USD", "english": "3", "notice": "14", **saved})
    return SETTINGS


def finalize_settings() -> None:
    """Cevapları kaydeder ve profile/aramaya uygular (terminal ve arayüz ortak)."""
    SETTINGS_FILE.write_text(json.dumps(SETTINGS, ensure_ascii=False, indent=2))
    PROFILE["resume_file"] = SETTINGS["resume"]
    if SETTINGS.get("cv_profile"):
        apply_cv_profile(SETTINGS["cv_profile"])
    log(f"CV: {Path(SETTINGS['resume']).name} | maaş: {', '.join(f'{v} {c}' for c, v in (SETTINGS.get('monthly_salary_by_currency') or {}).items()) + ' /ay' if SETTINGS.get('monthly_salary_by_currency') else SETTINGS['salary'] + ' /yıl'} | "
        f"İngilizce: {ENGLISH_LEVELS[SETTINGS['english']][0]} | ihbar: {SETTINGS['notice']} gün")


def api_key() -> str:
    key = find_key()
    if key:
        return key
    key = input("Ücretsiz OpenRouter anahtarı (openrouter.ai/settings/keys; sadece ücretsiz modeller kullanılır, ücret ödemezsin; boş = atla): ").strip()
    if key:
        save_key(key)
    return key


def save_key(key: str) -> None:
    env = HOME / ".env"
    lines = [l for l in (env.read_text().splitlines() if env.exists() else []) if not l.startswith("OPENROUTER_API_KEY=")]
    env.write_text("\n".join(lines + [f"OPENROUTER_API_KEY={key}"]) + "\n")


def find_key() -> str:
    """Kullanıcının kendi ücretsiz OpenRouter anahtarı: ortam değişkeni veya .env."""
    if os.getenv("OPENROUTER_API_KEY"):
        return os.environ["OPENROUTER_API_KEY"]
    env = HOME / ".env"
    if env.exists():
        m = re.search(r"^OPENROUTER_API_KEY=(.+)$", env.read_text(), re.M)
        if m:
            return m.group(1).strip().strip("'\"")
    return ""


def show_profile(prof: dict, locations: list[str]) -> None:
    print("\n=== CV'den çıkarılan profil ===")
    print(f"Ad: {prof['first_name']} {prof['last_name']} | {prof['email']} | {prof['phone_intl']}")
    print(f"Şehir: {prof['city']} | LinkedIn: {prof['linkedin'] or '-'} | GitHub: {prof['github'] or '-'}")
    print(f"Ünvan: {prof['current_title']} | Toplam deneyim: {prof['total_years']} yıl")
    print("Aranacak pozisyonlar:")
    for t in prof["job_titles"]:
        print(f"  - {t}")
    print("Başlıkta aranacak kelimeler: " + ", ".join(prof["title_keywords"]))
    print("Yetenek yılları: " + ", ".join(f"{x['skill']} {x['years']}y" for x in prof["skills"]))
    print("Ülkeler: " + ", ".join(locations))
    print(f"Ön yazı: {prof['cover_letter']}")


def cv_setup() -> None:
    """CV'den profil çıkarır (aynı CV için önbellekten) ve kullanıcıya onaylatır."""
    resume = Path(SETTINGS["resume"])
    digest = hashlib.sha256(resume.read_bytes()).hexdigest()
    prof = SETTINGS.get("cv_profile") if SETTINGS.get("cv_hash") == digest else None
    if not prof:
        key = api_key()
        if not key:
            SETTINGS.pop("cv_profile", None)
            return
        print("CV analiz ediliyor...")
        import cv_profile
        try:
            prof = cv_profile.extract(resume, key, log=print)
        except Exception as e:
            print(f"  ! CV analiz edilemedi ({e.__class__.__name__}: {str(e)[:150]}); config.yaml kullanılacak.")
            SETTINGS.pop("cv_profile", None)
            return
    locations = SETTINGS.get("locations") or CFG["search"]["locations"]
    while True:
        show_profile(prof, locations)
        choice = ask("Onaylıyor musun? e=evet, p=pozisyonları değiştir, u=ülkeleri değiştir, h=CV profilini kullanma", "e").lower()
        if choice == "e":
            SETTINGS.update(cv_profile=prof, cv_hash=digest, locations=locations)
            return
        if choice == "h":
            SETTINGS.pop("cv_profile", None)
            return
        if choice == "p":
            raw = input("Pozisyonlar (virgülle ayır): ")
            prof["job_titles"] = [t.strip() for t in raw.split(",") if t.strip()] or prof["job_titles"]
            words = {w.lower() for t in prof["job_titles"] for w in re.findall(r"[\w+#]{3,}", t)}
            prof["title_keywords"] = sorted(set(prof["title_keywords"]) | words)
        if choice == "u":
            raw = input("Ülkeler/bölgeler (virgülle ayır, ör. European Union, Türkiye): ")
            locations = [t.strip() for t in raw.split(",") if t.strip()] or locations


def apply_cv_profile(prof: dict) -> None:
    """Onaylanan CV profilini arama ayarlarına, filtrelere ve form cevaplarına uygular."""
    for k in ("first_name", "last_name", "email", "phone_intl", "city", "country", "current_title", "cover_letter"):
        if prof.get(k):
            PROFILE[k] = prof[k]
    # Linkler CV'de yoksa config'deki başkasının linki kullanılmasın
    PROFILE.update(linkedin=prof["linkedin"], github=prof["github"], current_company="")
    for k in ("linkedin", "github"):
        if PROFILE[k] and not PROFILE[k].startswith("http"):
            PROFILE[k] = "https://" + PROFILE[k]
    PROFILE["full_name"] = f"{PROFILE['first_name']} {PROFILE['last_name']}"
    if prof.get("phone_intl"):
        # Ülke kodu ayrı sorulduğu için yerel numara: "+90 534 ..." -> "534..."
        PROFILE["phone"] = re.sub(r"\D", "", re.sub(r"^\+\d{1,3}[\s-]", "", prof["phone_intl"]))
        code = re.match(r"\+(\d{1,3})[\s-]", prof["phone_intl"])
        PROFILE["country_code"] = f"(+{code.group(1)})" if code else ""
    years = prof["total_years"]
    search = CFG["search"]
    search["keywords"] = prof["job_titles"]
    search["locations"] = SETTINGS.get("locations") or search["locations"]
    search["experience_levels"] = [1, 2] if years < 2 else [2, 3, 4] if years < 6 else [3, 4, 5]
    words = {w for t in prof["job_titles"] for w in re.findall(r"[\w+#]+", t.lower()) if len(w) > 1}
    words |= {w.lower() for w in prof["title_keywords"]}
    CFG["filters"]["title_include"] = ["|".join(rf"\b{re.escape(w)}\b" for w in sorted(words))]
    # "AI Search uzmanı" gibi alakasız ilanlar elensin: başlıkta pozisyonun rol kelimesi de olmalı
    roles = {re.findall(r"[\w+#]+", t.lower())[-1] for t in prof["job_titles"] if re.findall(r"[\w+#]+", t)}
    roles |= {"engineer", "developer", "scientist", "analyst", "architect", "researcher", "specialist", "lead"} & words
    CFG["filters"]["title_require"] = ["|".join(rf"\b{re.escape(w)}" for w in sorted(roles))] if roles else []
    # Residence does not establish work authorization or willingness to commute.
    # AI-estimated skill years are not shown for confirmation in the GUI.
    # Keep only the user's explicit skill_years configuration, never CV estimates.


# ---------------------------------------------------------------- main

def process_job(page: Page, applied: dict, job_id: str) -> bool:
    """İlanı açar, uygunsa başvurur. Başvuru yapıldıysa True."""
    try:
        page.goto(f"https://www.linkedin.com/jobs/view/{job_id}/", wait_until="domcontentloaded")
        pause(2.5, 4.5)
        title, company, desc = job_info(page)
        reason = job_ok(title, company, desc) or ("applied_elsewhere" if applied_elsewhere(applied, title, company) else None)
        if reason:
            log(f"- atla [{reason}] {title} @ {company}")
            record(applied, job_id, title, company, reason)
            return False
        url = f"https://www.linkedin.com/jobs/view/{job_id}/"
        job = {"id": job_id, "title": title, "company": company, "url": url,
               "metadata": read_job_metadata(page, "linkedin", url)}
        if not check_job_eligibility(job, applied):
            return False
        log(f"> {title} @ {company}")
        status = apply_to_job(page, job_id, title, company, applied)
        if status == "no_easy_apply" and EXTERNAL:
            status = apply_external(page, job_id, title, company)
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
        log(f"  ! hata {job_id}: {e.__class__.__name__}: {str(e)[:150]}")
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
        log("Giriş yok. Açılan Chrome penceresinde LinkedIn'e giriş yap (10 dk bekleniyor)...")
        if "login" not in page.url:
            page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded")
        try:
            page.wait_for_url(re.compile(r"linkedin\.com/(feed|in/|mynetwork|jobs)"), timeout=600_000)
        except PWTimeout:
            log("LinkedIn'e giriş yapılmadı, diğer sitelere geçiliyor.")
            return False
        pause(3, 5)
    log("LinkedIn girişi tamam.")
    return True


def run_linkedin(page: Page, applied: dict, count: int) -> int:
    if not linkedin_login(page):
        return count
    stop = min(CFG["limits"]["max_applications_per_run"], count + CFG["limits"].get("per_source", 15))
    try:
        for job_ids in job_lists(page):
            for job_id in job_ids:
                if job_id in applied and not applied[job_id]["status"].startswith(RETRY_PREFIXES):
                    continue
                if count >= stop or time.time() > DEADLINE:
                    raise LimitReached
                if process_job(page, applied, job_id):
                    count += 1
    except LimitReached:
        log("Günlük limit/süre doldu.")
    except SessionLost:
        log("LinkedIn oturumu kapattı. Sonraki çalıştırmada açılan pencerede giriş yap; diğer sitelere geçiliyor.")
    return count


def launch(pw):
    """Kalıcı profil: sitelere bir kez elle giriş yapılır, oturumlar burada saklanır."""
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
    raise RuntimeError("Google Chrome bulunamadı. https://www.google.com/chrome adresinden yükleyip tekrar dene.") from last


def run_bot() -> int:
    """Başlangıç bilgileri (SETTINGS) hazırken başvuruları yapar."""
    global DEADLINE
    DEADLINE = time.time() + CFG["limits"]["max_runtime_minutes"] * 60
    STOP.clear()
    applied = load_applied()
    log(f"Başlıyor ({'test modu, başvuru gönderilmez' if DRY_RUN else 'gerçek başvuru'}), daha önce işlenen ilan: {len(applied)}")
    count = 0
    with sync_playwright() as pw:
        ctx = launch(pw)
        # Alan tıklama/doldurma 6 sn'de vazgeçsin (takılı kalmasın); sayfa yükleme 30 sn
        ctx.set_default_timeout(6000)
        ctx.set_default_navigation_timeout(30000)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            if CFG["sources"].get("linkedin", True):
                count = run_linkedin(page, applied, count)
            import sources
            count = sources.run(sys.modules[__name__], page, applied, count)
        except BrowserClosed:
            log("Chrome penceresi kapatıldı, durduruldu. Kalan ilanlar bir sonraki çalıştırmada denenir.")
        except Stopped:
            log("Durduruldu.")
        finally:
            try:
                ctx.close()
            except Exception:
                pass
    LAST_RUN_FILE.write_text(f"{dt.datetime.now().isoformat(timespec='minutes')} {'dry-run' if DRY_RUN else 'applied'}={count}\n")
    log(f"Bitti. {'Test edilen' if DRY_RUN else 'Başvurulan'}: {count}")
    return 0


def main() -> int:
    ask_settings()
    return run_bot()


if __name__ == "__main__":
    sys.exit(main())
