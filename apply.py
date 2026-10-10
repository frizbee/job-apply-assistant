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
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

import yaml
from playwright.sync_api import Locator, Page, sync_playwright
from playwright.sync_api import TimeoutError as PWTimeout

ROOT = Path(__file__).resolve().parent
CFG = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
APPLIED_FILE = ROOT / "data" / "applied.json"
LOG_FILE = ROOT / "data" / "applications.csv"
LAST_RUN_FILE = ROOT / "data" / "last_run.txt"
MANUAL_FILE = ROOT / "data" / "manual_apply.csv"
SHOTS = ROOT / "screenshots"
PROFILE_DIR = ROOT / ".browser-profile"
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
DEADLINE = time.time() + CFG["limits"]["max_runtime_minutes"] * 60
PROFILE = CFG["profile"]
EXTERNAL = CFG.get("external", {}).get("enabled", False)
SETTINGS_FILE = ROOT / "data" / "user_settings.json"
SETTINGS: dict = {}

# İngilizce seviyesi -> formlardaki seçeneklerde sırayla denenecek cevaplar
ENGLISH_LEVELS = {
    "1": ("Native / iki dilli", ["Native or bilingual", "Native", "Bilingual", "Fluent", "C2", "Yes"]),
    "2": ("Akıcı / profesyonel", ["Full professional", "Professional working", "Fluent", "Advanced", "C1", "Yes"]),
    "3": ("Orta / konuşma", ["Conversational", "Intermediate", "Limited working", "B2", "Komunikatywny", "Średnio zaawansowany", "Yes"]),
    "4": ("Temel", ["Elementary", "Basic", "Beginner", "A2", "No"]),
}

EASY_APPLY_RE = re.compile(r"easy apply|kolay başvuru", re.I)
PLACEHOLDER_RE = re.compile(r"select an option|seçenek belirleyin|seçin|seçiniz|wybierz|bitte wählen|choose an option", re.I)
OPEN_QUESTION_RE = re.compile(
    r"describe|explain|tell us|tell me|walk us|example|how (do|did|would|have) you|why|"
    r"what (is|was|are|were) your|share|elaborate|anlat|açıkla|neden", re.I)
YEARS_RE = re.compile(r"how many years|years of|yıl|ile lat|wie viele jahre|combien d.ann|cuántos años", re.I)

QUESTION_JS = r"""el => {
  const t = s => (s || '').replace(/\s+/g, ' ').trim();
  if (el.id) {
    const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
    if (l && t(l.innerText) && el.type !== 'radio' && el.type !== 'checkbox') return t(l.innerText);
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
RETRY_PREFIXES = ("skipped_", "modal_not_opened") + (("no_easy_apply",) if EXTERNAL else ())


class LimitReached(Exception):
    """Başvuru limiti veya süre doldu."""


class SessionLost(Exception):
    """LinkedIn oturumu sonlandırdı (çerez geçersiz)."""


def log(msg: str) -> None:
    print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", flush=True)


def pause(a: float = 1.5, b: float = 3.5) -> None:
    time.sleep(random.uniform(a, b))


# ---------------------------------------------------------------- state

def load_applied() -> dict:
    try:
        return json.loads(APPLIED_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def record(applied: dict, job_id: str, title: str, company: str, status: str) -> None:
    applied[job_id] = {"status": status, "date": dt.date.today().isoformat()}
    APPLIED_FILE.write_text(json.dumps(applied, indent=1, ensure_ascii=False))
    new_file = not LOG_FILE.exists()
    with LOG_FILE.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["date", "job_id", "title", "company", "status", "url"])
        w.writerow([dt.datetime.now().isoformat(timespec="minutes"), job_id, title, company,
                    status, f"https://www.linkedin.com/jobs/view/{job_id}/"])


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
    for rule in CFG["answers"]:
        if re.search(rule["match"], q):
            ans = rule["answer"]
            result = [str(a) for a in (ans if isinstance(ans, list) else [ans])]
            break
    if not result and (numeric or YEARS_RE.search(q)):
        for pattern, years in CFG["skill_years"].items():
            if re.search(pattern, q):
                result = [str(years)]
                break
        else:
            result = [str(PROFILE["default_years"])]
    if "__ENGLISH__" in result:
        result = ENGLISH_LEVELS[SETTINGS["english"]][1]
    result = [v for v in map(placeholder, result) if v]
    if numeric:
        nums = [re.sub(r"[^\d.]", "", a) for a in result]
        result = [n for n in nums if n] or [str(PROFILE["default_years"])]
    return result


def placeholder(a: str) -> str:
    """"__SALARY__" gibi yer tutucuları başlangıç cevaplarından / profilden doldurur."""
    m = re.fullmatch(r"__([A-Z_]+)__", a)
    if not m:
        return a
    key = m.group(1).lower()
    return str(SETTINGS.get(key) or PROFILE.get(key) or "")


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
    """Yıl aralıklı seçeneklerden (en az yarısı aralıksa) sayının düştüğünü, yoksa en yakınını seç."""
    ranges = [(i, year_range(o)) for i, o in opts]
    ranges = [(i, r) for i, r in ranges if r]
    if len(ranges) < max(2, len(opts) // 2):
        return None
    inside = [i for i, (lo, hi) in ranges if lo <= years <= hi or (lo <= years < hi)]
    if inside:
        return inside[-1]
    return min(ranges, key=lambda x: min(abs(years - x[1][0]), abs(years - x[1][1])))[0]


def choose_option(question: str, options: list[str]) -> int | None:
    """Seçenekler arasından cevabın index'ini döndür."""
    opts = [(i, o.strip()) for i, o in enumerate(options) if o.strip() and not PLACEHOLDER_RE.search(o)]
    if not opts:
        return None
    if YEARS_RE.search(question) or re.search(r"experience|deneyim|tecrübe", question, re.I):
        years = answer(question, numeric=True)
        idx = choose_years(float(years[0]), opts) if years else None
        if idx is not None:
            return idx
    for cand in answer(question) + [PROFILE["default_yes_no"]]:
        c = cand.lower()
        for i, o in opts:
            if o.lower() == c:
                return i
        for i, o in opts:
            if c in o.lower():
                return i
    return opts[0][0]


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


def fill_date(el: Locator) -> None:
    """Başlangıç tarihi gibi takvim alanlarına 2 hafta sonrasını yaz; geçerli olan biçimi bul."""
    d = dt.date.today() + dt.timedelta(days=14)
    formats = ["%m/%d/%Y", "%d.%m.%Y", "%d/%m/%Y"]
    if (el.get_attribute("lang") or "").startswith("tr"):
        formats.insert(0, formats.pop(1))
    for fmt in formats:
        el.fill(d.strftime(fmt))
        el.press("Tab")
        pause(0.4, 0.8)
        if el.get_attribute("aria-invalid") != "true":
            return
    log("    ! tarih alanı doldurulamadı")


def fill_radios(scope: Locator) -> list[str]:
    """Radyo butonlu sorular; cevaplanamayanları döndürür. Yeni LinkedIn formunda
    seçenekler div[role=radio] ve <label> boş; eski formda <legend> + <label for>."""
    unanswered: list[str] = []
    # İç içe gruplar iki kez gelebilir; ikincisi zaten seçili olduğu için atlanır
    for fs in scope.locator("fieldset:has(input[type='radio']), [role='radiogroup']").all():
        try:
            if fs.locator("input[type='radio']:checked, [role='radio'][aria-checked='true']").count():
                continue
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
            log(f"    ! radio doldurulamadı: {e.__class__.__name__}")
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
    cands = [std] if std else answer(q) + [PROFILE["default_yes_no"]]
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
        idx = choose_option(q, texts) if not std else 0
        opts.nth(idx or 0).click()
        pause(0.3, 0.7)
        return True
    except Exception as e:
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
    for i, (q, picked) in enumerate(scope.evaluate(YESNO_JS)):
        if picked:
            continue
        idx = choose_option(q, ["Yes", "No"])
        try:
            scope.locator(f"[data-yesno='{i}'] button").nth(idx or 0).click()
            pause(0.3, 0.6)
        except Exception:
            unanswered.append(q)
    return unanswered


def fill_checkgroups(scope: Locator) -> None:
    """Çoklu seçimde config'teki kurala, yoksa skill_years'taki becerilere uyan seçenekleri işaretler."""
    for i, (q, opts, any_checked) in enumerate(scope.evaluate(CHECKGROUP_JS)):
        if any_checked:
            continue
        cands = [c.lower() for c in answer(q)]
        pick = [j for j, o in enumerate(opts) if any(c and c in o.lower() for c in cands)]
        if not pick:
            pick = [j for j, o in enumerate(opts)
                    if any(re.search(pat, o.lower()) for pat in CFG["skill_years"])]
        if not pick:
            pick = [j for j, o in enumerate(opts) if not re.search(r"none|other|hiçbiri|diğer", o, re.I)][:1]
        cbs = scope.locator(f"[data-checkgroup='{i}'] input[type='checkbox']")
        for j in pick:
            try:
                cb = cbs.nth(j)
                row = cb.locator("xpath=ancestor::*[normalize-space(.)!=''][1]")
                try:
                    row.click(timeout=4000)
                except PWTimeout:
                    cb.check(force=True)
                if not cb.is_checked():
                    cb.check(force=True)
            except Exception as e:
                log(f"    ! onay kutusu seçilemedi: {e.__class__.__name__}")


def standard_answer(el: Locator, q: str) -> str | None:
    """Dış sitelerdeki ad/e-posta/telefon gibi standart alanlar."""
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
        "input[type='number']:visible, input[type='url']:visible, input:not([type]):visible, textarea:visible"
    )
    for el in fields.all():
        try:
            invalid = el.get_attribute("aria-invalid") == "true"
            if el.input_value().strip() and not invalid:
                continue
            if el.get_attribute("data-testid") == "date-picker-input":
                fill_date(el)
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
            if not cands and OPEN_QUESTION_RE.search(q):
                # Açık uçlu soru: uzun metin alan alanlara ön yazıyı yaz
                maxlen = int(el.get_attribute("maxlength") or 0)
                is_long = el.evaluate("e => e.tagName") == "TEXTAREA" or not maxlen or maxlen >= len(PROFILE["cover_letter"])
                if is_long:
                    log(f"    ~ ön yazı ile cevaplandı: {q[:80]}")
                    cands = [PROFILE["cover_letter"]]
            if not cands:
                log(f"    ? cevapsız soru: {q[:90]}")
                unanswered.append(q)
                continue
            el.fill(cands[0])
            pause(0.6, 1.2)
            # Şehir gibi typeahead alanlarında ilk öneriyi seç
            sugg = page.locator("[role='listbox'] [role='option']:visible").first
            if sugg.count():
                sugg.click()
        except Exception as e:
            log(f"    ! alan doldurulamadı: {e.__class__.__name__}")

    # Açılır listeler
    for sel in modal.locator("select:visible").all():
        try:
            current = sel.evaluate("s => s.options[s.selectedIndex] ? s.options[s.selectedIndex].text : ''")
            value = sel.input_value()
            if current.strip() and value and not PLACEHOLDER_RE.search(current) and not PLACEHOLDER_RE.search(value):
                continue
            options = sel.locator("option").all_inner_texts()
            idx = choose_option(question_of(sel), options)
            if idx is not None:
                sel.select_option(index=idx)
        except Exception as e:
            log(f"    ! select doldurulamadı: {e.__class__.__name__}")

    unanswered += fill_radios(modal)

    # Onay kutuları (şartları kabul et vb.)
    for cb in modal.locator("input[type='checkbox']:not(:checked)").all():
        q = question_of(cb).lower()
        if re.search(r"agree|consent|terms|privacy|acknowledge|confirm", q):
            try:
                cb.check(force=True)
            except Exception:
                pass

    resume = ROOT / PROFILE["resume_file"]
    if external:
        unanswered += fill_yesno(modal)
        fill_checkgroups(modal)
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
        return unanswered

    # CV: başta seçilen dosya LinkedIn'de listede yoksa yükle (yüklenen otomatik seçilir)
    if resume.exists() and modal.locator("input[type='file']").count():
        if resume.name.lower() not in modal.inner_text().lower():
            try:
                modal.locator("input[type='file']").first.set_input_files(str(resume))
                pause(2, 3)
            except Exception:
                pass
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


def apply_to_job(page: Page, job_id: str) -> str:
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
        fill_step(page, modal)
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
            primary.click()
            pause(3, 5)
            page.keyboard.press("Escape")
            return "applied"

        before = modal.inner_text()
        primary.click()
        pause(1.5, 3)
        if has_errors(modal) or modal.inner_text() == before:
            fill_step(page, modal)  # bir kez daha dene
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
    "greenhouse": re.compile(r"greenhouse\.io"),
    "lever": re.compile(r"jobs\.(eu\.)?lever\.co"),
    "ashby": re.compile(r"jobs\.ashbyhq\.com"),
}
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


def external_url(page: Page) -> str | None:
    """LinkedIn'deki 'Başvur' butonuna basıp şirket sitesinin adresini döndürür."""
    btn = page.get_by_role("button", name=APPLY_BTN_RE).or_(page.get_by_role("link", name=APPLY_BTN_RE))
    btn = btn.filter(has_not_text=EASY_APPLY_RE).first
    if not btn.count():
        return None
    ctx = page.context
    before = len(ctx.pages)
    btn.click()
    for _ in range(30):
        time.sleep(0.5)
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
    for name, rx in ATS.items():
        if rx.search(url):
            break
    else:
        # Şirket sitesine gömülü Greenhouse/Ashby/Lever iframe'i
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            pause(3, 4)
        except Exception:
            return None, "unknown"
        for fr in page.frames:
            for name, rx in ATS.items():
                if rx.search(fr.url):
                    url = fr.url
                    break
            else:
                continue
            break
        else:
            return None, "unknown"
    if name == "lever" and not re.search(r"/apply/?(\?|$)", url):
        url = re.sub(r"/?(\?.*)?$", r"/apply\1", url, count=1)
    elif name == "ashby" and "/application" not in url:
        url = re.sub(r"/?(\?.*)?$", r"/application\1", url, count=1)
    return url, name


def apply_external(page: Page, job_id: str, title: str, company: str) -> str:
    url = external_url(page)
    if not url:
        return "no_apply_button"
    form_url, ats = ats_apply_url(page, url)
    if not form_url:
        record_manual(job_id, title, company, url, "unsupported_site")
        return "external_manual"
    log(f"  -> {ats}: {form_url[:90]}")
    page.goto(form_url, wait_until="domcontentloaded", timeout=30000)
    pause(3, 5)
    if ats == "ashby":
        tab = page.get_by_role("tab", name=re.compile(r"application", re.I))
        if tab.count():
            tab.first.click()
            pause(1.5, 2.5)
    form = page.locator("form:has(input[type='file']), form:has(textarea), form:has(input[type='email'])").first
    if not form.count():
        form = page.locator("body")

    unanswered = fill_step(page, form, external=True)
    pause(1, 2)
    unanswered = fill_step(page, form, external=True)  # açılan koşullu sorular için ikinci tur
    required_empty = form.locator(
        "input[required]:visible, textarea[required]:visible, [aria-required='true']:visible"
    ).evaluate_all("els => els.filter(e => 'value' in e && !e.value.trim() && e.type !== 'file').length")
    if unanswered and required_empty:
        screenshot(page, f"{job_id}_ext_unanswered")
        record_manual(job_id, title, company, form_url, "unanswered: " + " | ".join(unanswered)[:300])
        return "external_unanswered"

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
    if any(re.search(p, t) for p in f["title_exclude"]):
        return "title_excluded"
    if any(re.search(p, d) for p in f.get("description_exclude", [])):
        return "description_excluded"
    if any(c.lower() in company.lower() for c in f.get("exclude_companies", [])):
        return "company_excluded"
    return None


# ---------------------------------------------------------------- başlangıç soruları

def ask(prompt: str, default: str) -> str:
    raw = input(f"{prompt} [{default}]: " if default else f"{prompt}: ").strip()
    return raw or default


def ask_settings() -> None:
    """CV, maaş ve İngilizce bilgisini kullanıcıdan alır; cevaplar bir sonraki sefere varsayılan olur."""
    saved = json.loads(SETTINGS_FILE.read_text()) if SETTINGS_FILE.exists() else {}
    SETTINGS.update({
        "resume": PROFILE["resume_file"], "salary": "75000", "english": "3", "notice": "14", **saved,
    })
    if not sys.stdin.isatty():
        log("Terminal yok, kayıtlı ayarlar kullanılıyor.")
    else:
        print("\n=== Başvuru bilgileri (Enter = köşeli parantezdeki değer) ===")
        while True:
            raw = ask("CV dosyası (PDF/DOCX yolu, dosyayı terminale sürükleyebilirsin)", SETTINGS["resume"])
            path = Path(raw.strip("'\" ").replace("\\ ", " ")).expanduser()
            path = path if path.is_absolute() else ROOT / path
            if path.is_file() and path.suffix.lower() in (".pdf", ".doc", ".docx"):
                SETTINGS["resume"] = str(path)
                break
            print(f"  ! Dosya bulunamadı veya PDF/DOCX değil: {path}")
        while True:
            salary = re.sub(r"[^\d]", "", ask("Yıllık maaş beklentisi (USD/EUR, sadece sayı)", SETTINGS["salary"]))
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
        SETTINGS_FILE.parent.mkdir(exist_ok=True)
        SETTINGS_FILE.write_text(json.dumps(SETTINGS, ensure_ascii=False, indent=2))
        print()
    PROFILE["resume_file"] = SETTINGS["resume"]
    if SETTINGS.get("cv_profile"):
        apply_cv_profile(SETTINGS["cv_profile"])
    log(f"CV: {Path(SETTINGS['resume']).name} | maaş: {SETTINGS['salary']} | "
        f"İngilizce: {ENGLISH_LEVELS[SETTINGS['english']][0]} | ihbar: {SETTINGS['notice']} gün")


def api_key() -> str:
    """ANTHROPIC_API_KEY ortamdan veya .env'den; yoksa sorulup .env'e yazılır."""
    if os.getenv("ANTHROPIC_API_KEY"):
        return os.environ["ANTHROPIC_API_KEY"]
    env = ROOT / ".env"
    if env.exists():
        m = re.search(r"^ANTHROPIC_API_KEY=(.+)$", env.read_text(), re.M)
        if m:
            return m.group(1).strip().strip("'\"")
    key = input("Claude API anahtarı (CV analizi için; boş bırakırsan config.yaml kullanılır): ").strip()
    if key:
        with env.open("a") as f:
            f.write(f"ANTHROPIC_API_KEY={key}\n")
    return key


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
            prof = cv_profile.extract(resume, key)
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
    PROFILE["full_name"] = f"{PROFILE['first_name']} {PROFILE['last_name']}"
    if prof.get("phone_intl"):
        # Ülke kodu ayrı sorulduğu için yerel numara: "+90 534 ..." -> "534..."
        PROFILE["phone"] = re.sub(r"\D", "", re.sub(r"^\+\d{1,3}[\s-]", "", prof["phone_intl"]))
    years = prof["total_years"]
    PROFILE["default_years"] = max(1, min(years, 3))
    search = CFG["search"]
    search["keywords"] = prof["job_titles"]
    search["locations"] = SETTINGS.get("locations") or search["locations"]
    search["experience_levels"] = [1, 2] if years < 2 else [2, 3, 4] if years < 6 else [3, 4, 5]
    CFG["filters"]["title_include"] = ["|".join(re.escape(w.lower()) for w in prof["title_keywords"])]
    CFG["skill_years"] = {re.escape(x["skill"].lower()): x["years"] for x in prof["skills"]}


# ---------------------------------------------------------------- main

def process_job(page: Page, applied: dict, job_id: str) -> bool:
    """İlanı açar, uygunsa başvurur. Başvuru yapıldıysa True."""
    try:
        page.goto(f"https://www.linkedin.com/jobs/view/{job_id}/", wait_until="domcontentloaded")
        pause(2.5, 4.5)
        title, company, desc = job_info(page)
        reason = job_ok(title, company, desc)
        if reason:
            log(f"- atla [{reason}] {title} @ {company}")
            record(applied, job_id, title, company, reason)
            return False
        log(f"> {title} @ {company}")
        status = apply_to_job(page, job_id)
        if status == "no_easy_apply" and EXTERNAL:
            status = apply_external(page, job_id, title, company)
        log(f"  = {status}")
        if status != "dry_run":
            record(applied, job_id, title, company, status)
        pause(4, 9)
        return status in ("applied", "applied_external", "dry_run")
    except Exception as e:
        if "TOO_MANY_REDIRECTS" in str(e):
            raise SessionLost() from None
        log(f"  ! hata {job_id}: {e.__class__.__name__}: {str(e)[:150]}")
        screenshot(page, f"{job_id}_error")
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        return False


def main() -> int:
    ask_settings()
    applied = load_applied()
    log(f"Başlıyor (DRY_RUN={DRY_RUN}), daha önce işlenen ilan: {len(applied)}")
    count = 0

    with sync_playwright() as pw:
        # Kalıcı tarayıcı profili: LinkedIn'e bir kez elle giriş yapılır, oturum burada saklanır.
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            channel="chrome",
            headless=os.getenv("HEADLESS", "false").lower() == "true",
            locale="en-US",
            viewport={"width": 1366, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.set_default_timeout(15000)

        page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded")
        pause(2, 4)
        if re.search(r"login|checkpoint|authwall|signup|uas/", page.url) or page.url.rstrip("/") == "https://www.linkedin.com":
            log("Giriş yok. Açılan Chrome penceresinde LinkedIn'e giriş yap (10 dk bekleniyor)...")
            if "login" not in page.url:
                page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded")
            try:
                page.wait_for_url(re.compile(r"linkedin\.com/(feed|in/|mynetwork|jobs)"), timeout=600_000)
            except PWTimeout:
                log("Giriş yapılmadı, çıkılıyor.")
                ctx.close()
                return 1
            pause(3, 5)
        log("Giriş başarılı.")

        try:
            for job_ids in job_lists(page):
                for job_id in job_ids:
                    if job_id in applied and not applied[job_id]["status"].startswith(RETRY_PREFIXES):
                        continue
                    if count >= CFG["limits"]["max_applications_per_run"] or time.time() > DEADLINE:
                        raise LimitReached
                    if process_job(page, applied, job_id):
                        count += 1
        except LimitReached:
            log("Günlük limit/süre doldu.")
        except SessionLost:
            log("LinkedIn oturumu kapattı. Tekrar çalıştırıp açılan pencerede giriş yap.")
            ctx.close()
            return 1

        ctx.close()

    LAST_RUN_FILE.write_text(f"{dt.datetime.now().isoformat(timespec='minutes')} {'dry-run' if DRY_RUN else 'applied'}={count}\n")
    log(f"Bitti. {'Test edilen' if DRY_RUN else 'Başvurulan'}: {count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
