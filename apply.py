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
PLACEHOLDER_RE = re.compile(r"select an option|seçin|seçiniz", re.I)
YEARS_RE = re.compile(r"how many years|years of|yıl", re.I)

QUESTION_JS = """el => {
  const t = s => (s || '').replace(/\\s+/g, ' ').trim();
  if (el.id) {
    const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
    if (l && t(l.innerText)) return t(l.innerText);
  }
  const fs = el.closest('fieldset');
  if (fs) { const lg = fs.querySelector('legend'); if (lg) return t(lg.innerText); }
  const grp = el.closest('[data-test-form-element], .jobs-easy-apply-form-element, .fb-dash-form-element');
  if (grp) return t(grp.innerText).slice(0, 300);
  return t(el.getAttribute('aria-label') || el.getAttribute('placeholder'));
}"""


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
            if numeric:
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

def fill_step(page: Page, modal: Locator) -> None:
    # Metin alanları
    fields = modal.locator(
        "input[type='text']:visible, input[type='tel']:visible, input[type='email']:visible, "
        "input[type='number']:visible, input:not([type]):visible, textarea:visible"
    )
    for el in fields.all():
        try:
            if el.input_value().strip():
                continue
            q = question_of(el)
            numeric = el.get_attribute("type") == "number" or "numeric" in (el.get_attribute("id") or "")
            cands = answer(q, numeric=numeric)
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
            if current.strip() and not PLACEHOLDER_RE.search(current):
                continue
            options = sel.locator("option").all_inner_texts()
            idx = choose_option(question_of(sel), options)
            if idx is not None:
                sel.select_option(index=idx)
        except Exception as e:
            log(f"    ! select doldurulamadı: {e.__class__.__name__}")

    # Radyo butonlu sorular
    for fs in modal.locator("fieldset:visible").all():
        try:
            if not fs.locator("input[type='radio']").count() or fs.locator("input[type='radio']:checked").count():
                continue
            labels = fs.locator("label")
            legend = fs.locator("legend").first
            q = legend.inner_text() if legend.count() else ""
            idx = choose_option(q, labels.all_inner_texts())
            if idx is not None:
                labels.nth(idx).click()
        except Exception as e:
            log(f"    ! radio doldurulamadı: {e.__class__.__name__}")

    # Onay kutuları (şartları kabul et vb.)
    for cb in modal.locator("fieldset input[type='checkbox']:not(:checked)").all():
        q = question_of(cb).lower()
        if re.search(r"agree|consent|terms|privacy|acknowledge|confirm", q):
            try:
                cb.check(force=True)
            except Exception:
                pass

    # CV: sadece hiç kayıtlı CV yoksa yükle
    resume = ROOT / PROFILE["resume_file"]
    if resume.exists() and modal.locator("input[type='file']").count():
        has_doc = modal.locator(".jobs-document-upload-redesign-card__container, [class*='document-upload'][class*='card']").count()
        if not has_doc:
            try:
                modal.locator("input[type='file']").first.set_input_files(str(resume))
                pause(2, 3)
            except Exception:
                pass


def has_errors(modal: Locator) -> bool:
    return modal.locator(".artdeco-inline-feedback--error:visible, [data-test-form-element-error-messages]:visible").count() > 0


def discard(page: Page) -> None:
    try:
        page.locator("div[role='dialog'] button[aria-label='Dismiss']").first.click(timeout=4000)
        pause(1, 2)
        page.locator(
            "button[data-control-name='discard_application_confirm_btn'], "
            "button[data-test-dialog-secondary-btn], button:has-text('Discard')"
        ).first.click(timeout=4000)
    except PWTimeout:
        page.keyboard.press("Escape")
    pause(1, 2)


def apply_to_job(page: Page, job_id: str) -> str:
    btn = page.locator("button.jobs-apply-button:visible").first
    if not btn.count() or not EASY_APPLY_RE.search(btn.inner_text() + (btn.get_attribute("aria-label") or "")):
        return "no_easy_apply"
    btn.click()
    modal = page.locator("div[role='dialog']").first
    try:
        modal.wait_for(timeout=10000)
    except PWTimeout:
        return "modal_not_opened"

    for _ in range(15):
        pause(1, 2)
        fill_step(page, modal)
        follow = modal.locator("input#follow-company-checkbox:checked")
        if follow.count():
            modal.locator("label[for='follow-company-checkbox']").click()

        primary = modal.locator("footer button.artdeco-button--primary:visible, button.artdeco-button--primary:visible").last
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

        primary.click()
        pause(1.5, 3)
        if has_errors(modal):
            fill_step(page, modal)  # bir kez daha dene
            primary.click()
            pause(1.5, 3)
            if has_errors(modal):
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


def collect_job_ids(page: Page) -> list[str]:
    ids: list[str] = []
    for kw in CFG["search"]["keywords"]:
        for loc in CFG["search"]["locations"]:
            for p in range(CFG["search"]["pages_per_search"]):
                try:
                    page.goto(search_url(kw, loc) + f"&start={p * 25}", wait_until="domcontentloaded")
                except Exception as e:
                    if "TOO_MANY_REDIRECTS" in str(e):
                        raise SessionLost() from None
                    log(f"  ! arama açılamadı: {str(e)[:100]}")
                    break
                pause(3, 5)
                found = page.evaluate("""async () => {
                    const cards = [...document.querySelectorAll('[data-occludable-job-id], [data-job-id]')];
                    for (const c of cards) { c.scrollIntoView(); await new Promise(r => setTimeout(r, 120)); }
                    return cards.map(c => c.getAttribute('data-occludable-job-id') || c.getAttribute('data-job-id'));
                }""")
                found = [f for f in found if f and f.isdigit()]
                new = [f for f in found if f not in ids]
                ids.extend(new)
                log(f"  '{kw}' @ {loc} s{p + 1}: {len(found)} ilan ({len(new)} yeni)")
                if len(found) < 25:
                    break
    return ids


def first_text(page: Page, selectors: list[str]) -> str:
    for sel in selectors:
        loc = page.locator(sel).first
        if loc.count():
            try:
                return loc.inner_text(timeout=3000).strip()
            except PWTimeout:
                pass
    return ""


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
            job_ids = [j for j in collect_job_ids(page) if j not in applied]
        except SessionLost:
            log("LinkedIn oturumu kapattı. Tekrar çalıştırıp açılan pencerede giriş yap.")
            ctx.close()
            return 1
        log(f"İşlenecek yeni ilan: {len(job_ids)}")

        for job_id in job_ids:
            if count >= CFG["limits"]["max_applications_per_run"] or time.time() > DEADLINE:
                log("Günlük limit/süre doldu.")
                break
            try:
                page.goto(f"https://www.linkedin.com/jobs/view/{job_id}/", wait_until="domcontentloaded")
                pause(2.5, 4.5)
                title = first_text(page, [".job-details-jobs-unified-top-card__job-title", "h1"])
                company = first_text(page, [".job-details-jobs-unified-top-card__company-name",
                                            ".jobs-unified-top-card__company-name"])
                desc = first_text(page, ["#job-details", ".jobs-description"])
                reason = job_ok(title, company, desc)
                if reason:
                    log(f"- atla [{reason}] {title} @ {company}")
                    record(applied, job_id, title, company, reason)
                    continue
                log(f"> {title} @ {company}")
                status = apply_to_job(page, job_id)
                log(f"  = {status}")
                if status != "dry_run":
                    record(applied, job_id, title, company, status)
                if status in ("applied", "dry_run"):
                    count += 1
                pause(4, 9)
            except Exception as e:
                log(f"  ! hata {job_id}: {e.__class__.__name__}: {str(e)[:150]}")
                screenshot(page, f"{job_id}_error")
                try:
                    page.keyboard.press("Escape")
                except Exception:
                    pass

        ctx.close()

    LAST_RUN_FILE.write_text(f"{dt.datetime.now().isoformat(timespec='minutes')} {'dry-run' if DRY_RUN else 'applied'}={count}\n")
    log(f"Bitti. {'Test edilen' if DRY_RUN else 'Başvurulan'}: {count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
