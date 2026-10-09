#!/usr/bin/env python3
"""LinkedIn Easy Apply botu.

Son 24 saatteki AI ilanlarını tarar, config.yaml'daki filtrelere uyanlara
Easy Apply ile başvurur. Başvurulan ilanlar data/applied.json'da tutulur,
böylece aynı ilana iki kez başvurulmaz.

Oturum .browser-profile/ içinde saklanır; ilk çalıştırmada açılan Chrome
penceresinde LinkedIn'e bir kez giriş yapılır.

Ortam değişkenleri:
  DRY_RUN   "true" ise formları doldurur ama son adımda göndermez
  HEADLESS  "true" ise pencere açmadan çalışır
"""
import csv
import datetime as dt
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
SHOTS = ROOT / "screenshots"
PROFILE_DIR = ROOT / ".browser-profile"
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
DEADLINE = time.time() + CFG["limits"]["max_runtime_minutes"] * 60
PROFILE = CFG["profile"]

EASY_APPLY_RE = re.compile(r"easy apply|kolay başvuru", re.I)
PLACEHOLDER_RE = re.compile(r"select an option|seçenek belirleyin|seçin|seçiniz|wybierz|bitte wählen|choose an option", re.I)
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
RETRY_PREFIXES = ("skipped_", "modal_not_opened")


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
        page.screenshot(path=str(SHOTS / f"{name}.png"))
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
    result = [PROFILE["cover_letter"] if a == "__COVER_LETTER__" else a for a in result]
    if numeric:
        nums = [re.sub(r"[^\d.]", "", a) for a in result]
        result = [n for n in nums if n] or [str(PROFILE["default_years"])]
    return result


def choose_option(question: str, options: list[str]) -> int | None:
    """Seçenekler arasından cevabın index'ini döndür."""
    opts = [(i, o.strip()) for i, o in enumerate(options) if o.strip() and not PLACEHOLDER_RE.search(o)]
    if not opts:
        return None
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


def fill_step(page: Page, modal: Locator) -> None:
    # Metin alanları
    fields = modal.locator(
        "input[type='text']:visible, input[type='tel']:visible, input[type='email']:visible, "
        "input[type='number']:visible, input:not([type]):visible, textarea:visible"
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
            numeric = el.get_attribute("type") == "number" or "numeric" in (el.get_attribute("id") or "")
            # Dolu ama geçersiz alan genelde sayı bekliyor
            cands = answer(q, numeric=numeric or invalid)
            if not cands:
                log(f"    ? cevapsız soru: {q[:90]}")
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

    # Radyo butonlu sorular. Yeni formda seçenekler div[role=radio], <label> boş;
    # eski formda <legend> + <label for>.
    for fs in modal.locator("fieldset:has(input[type='radio'])").all():
        try:
            if fs.locator("input[type='radio']:checked, [role='radio'][aria-checked='true']").count():
                continue
            q, labels = fs.evaluate(RADIO_GROUP_JS)
            idx = choose_option(q, labels)
            if idx is None:
                log(f"    ? cevapsız soru: {q[:90]}")
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

    # Onay kutuları (şartları kabul et vb.)
    for cb in modal.locator("input[type='checkbox']:not(:checked)").all():
        q = question_of(cb).lower()
        if re.search(r"agree|consent|terms|privacy|acknowledge|confirm", q):
            try:
                cb.check(force=True)
            except Exception:
                pass

    # CV: sadece hiç kayıtlı CV yoksa yükle
    resume = ROOT / PROFILE["resume_file"]
    if resume.exists() and modal.locator("input[type='file']").count():
        has_doc = re.search(r"\.pdf|\.docx?", modal.inner_text(), re.I)
        if not has_doc:
            try:
                modal.locator("input[type='file']").first.set_input_files(str(resume))
                pause(2, 3)
            except Exception:
                pass


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


# ---------------------------------------------------------------- search

def search_url(keyword: str, location: str) -> str:
    s = CFG["search"]
    params = {
        "keywords": keyword,
        "location": location,
        "f_AL": "true",  # sadece Easy Apply
        "f_TPR": f"r{s['posted_within_hours'] * 3600}",
        "sortBy": "DD",
    }
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
        log(f"  = {status}")
        if status != "dry_run":
            record(applied, job_id, title, company, status)
        pause(4, 9)
        return status in ("applied", "dry_run")
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
