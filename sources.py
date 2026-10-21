"""LinkedIn dışındaki iş kaynakları.

- Wellfound ve Indeed: sitenin kendi hızlı başvurusu (giriş gerekir; ilk seferde pencerede yapılır)
- Remote OK, Himalayas, We Work Remotely: ilanlar API/RSS'ten toplanır, başvuru şirket
  sitesindeki forma gider (apply.apply_form: Greenhouse/Lever/Ashby, sınırlı ve aralıklı;
  diğerleri data/manual_apply.csv'ye yazılır)

Her kaynak ilanları üretir; ortak döngü filtreler, aynı ilanı ikinci kez başvurmaz
(farklı sitelerde aynı şirket+başlık dahil), sonucu kaydeder.
"""
import datetime as dt
import html
import json
import re
import time
import urllib.request
import xml.etree.ElementTree as ET
import job_metadata
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus, urlparse

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/141.0 Safari/537.36")
GENERIC_WORDS = {"and", "the", "of", "for", "with", "senior", "junior", "remote"}
LOGGED_IN: dict[str, bool] = {}
core = None  # apply modülü; run() içinde atanır


# ---------------------------------------------------------------- yardımcılar

def http(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json, text/xml, */*"})
    from cv_profile import SSL_CTX
    with urllib.request.urlopen(req, timeout=30, context=SSL_CTX) as r:
        return r.read().decode("utf-8", "ignore")


def strip_html(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def fresh(ts: dt.datetime | float | None) -> bool:
    return job_metadata.eligibility_reason({"posted_at": ts},
        core.CFG["search"]["posted_within_hours"], remote_only=False) is None


def keyword_match(title: str) -> bool:
    """Arama sorgusu olmayan kaynaklarda başlık, aranan pozisyonlardan birinin tüm kelimelerini içermeli."""
    t = title.lower()
    for kw in core.CFG["search"]["keywords"]:
        words = [w for w in re.findall(r"[\w+#.]+", kw.lower()) if w not in GENERIC_WORDS]
        if words and all(re.search(rf"(?<!\w){re.escape(w)}(?!\w)", t) for w in words):
            return True
    return False


def location_ok(text: str) -> bool:
    return core.job_location_ok(text)


def page_ready(page) -> bool:
    return not core.blocked(page)


def goto(page, url: str, what: str) -> bool:
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    core.pause(2, 4)
    return core.wait_human(page, page_ready, f"{what} güvenlik doğrulamasını geç")


def click_apply(page) -> str | None:
    btn = page.get_by_role("link", name=core.APPLY_BTN_RE).or_(page.get_by_role("button", name=core.APPLY_BTN_RE))
    return core.follow_apply(page, btn.first)


def external(page, job: dict, url: str | None, board: str, applied: dict | None = None) -> str:
    if not url or board in url:
        core.record_manual(job["id"], job["title"], job["company"], job["url"], "apply_link_not_found")
        return "external_manual"
    return core.apply_form(page, job["id"], job["title"], job["company"], url, applied)


# ---------------------------------------------------------------- Remote OK

def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def remoteok(page):
    feeds = [""] + [f"?tag={slug(kw)}" for kw in core.CFG["search"]["keywords"]] + ["?tag=ai", "?tag=machine-learning"]
    jobs = {}
    for q in dict.fromkeys(feeds):
        for j in json.loads(http(f"https://remoteok.com/api{q}"))[1:]:
            jobs[j["id"]] = j
        time.sleep(1)
    for j in jobs.values():
        if not keyword_match(j.get("position", "")):
            continue
        job = {"id": f"remoteok:{j['id']}", "title": j["position"], "company": j.get("company", ""),
               "url": j["url"], "desc": strip_html(j.get("description", "")), "location": j.get("location", ""),
               "metadata": {"posted_at": j.get("epoch") or j.get("date"),
                            "workplace": job_metadata.workplace_kind(j.get("workplaceType"), j.get("remote"), j.get("location")),
                            "location": j.get("location", "")}}

        def apply(applied=None, job=job):
            if not goto(page, job["url"], "Remote OK"):
                return "blocked"
            return external(page, job, click_apply(page), "remoteok.com", applied)
        yield job, apply


# ---------------------------------------------------------------- Himalayas

def himalayas(page):
    seen = set()
    for kw in core.CFG["search"]["keywords"]:
        data = json.loads(http(f"https://himalayas.app/jobs/api/search?q={quote_plus(kw)}&limit=20"))
        for j in data.get("jobs", []):
            if j["guid"] in seen or not keyword_match(j["title"]):
                continue
            seen.add(j["guid"])
            job = {"id": "himalayas:" + j["guid"].rsplit("/companies/", 1)[-1], "title": j["title"],
                   "company": j.get("companyName", ""), "url": j["applicationLink"],
                   "desc": strip_html(j.get("description", "")),
                   "location": ", ".join(j.get("locationRestrictions") or []),
                   "metadata": {"posted_at": j.get("pubDate"),
                                "workplace": job_metadata.workplace_kind(j.get("workplaceType"), j.get("remote")),
                                "location": ", ".join(j.get("locationRestrictions") or [])}}

            def apply(applied=None, job=job):
                if not goto(page, job["url"], "Himalayas"):
                    return "blocked"
                return external(page, job, click_apply(page), "himalayas.app", applied)
            yield job, apply


# ---------------------------------------------------------------- We Work Remotely

def wwr_login(page) -> bool:
    if "wwr" not in LOGGED_IN:
        page.goto("https://weworkremotely.com/job-seekers/account/login", wait_until="domcontentloaded")
        LOGGED_IN["wwr"] = core.wait_human(
            page, lambda p: not re.search(r"account/(login|register|sign)", p.url) and page_ready(p),
            "We Work Remotely'ye giriş yap (ücretsiz hesap; başvuru linkleri girişle görünüyor)")
    return LOGGED_IN["wwr"]


def weworkremotely(page):
    root = ET.fromstring(http("https://weworkremotely.com/remote-jobs.rss"))
    for item in root.iter("item"):
        company, _, title = (item.findtext("title") or "").partition(": ")
        try:
            posted = parsedate_to_datetime(item.findtext("pubDate"))
        except (TypeError, ValueError):
            posted = None
        if not title or not keyword_match(title):
            continue
        link = item.findtext("link") or ""
        job = {"id": "wwr:" + link.rstrip("/").rsplit("/", 1)[-1], "title": title, "company": company,
               "url": link, "desc": strip_html(item.findtext("description")), "location": item.findtext("region") or "",
               "metadata": {"posted_at": posted,
                            "workplace": job_metadata.workplace_kind(item.findtext("workplaceType"), item.findtext("region")),
                            "location": item.findtext("region") or ""}}

        def apply(applied=None, job=job):
            if not goto(page, job["url"], "We Work Remotely"):
                return "blocked"
            if page.locator("a[href*='account/register']").count():
                if not wwr_login(page) or not goto(page, job["url"], "We Work Remotely"):
                    return "blocked"
            return external(page, job, click_apply(page), "weworkremotely.com", applied)
        yield job, apply


# ---------------------------------------------------------------- Wellfound

def wellfound_login(page) -> bool:
    if "wellfound" not in LOGGED_IN:
        logged = lambda p: page_ready(p) and not p.get_by_role("link", name=re.compile(r"^log ?in$", re.I)).count()
        page.goto("https://wellfound.com/jobs", wait_until="domcontentloaded")
        core.pause(2, 4)
        if not logged(page):
            page.goto("https://wellfound.com/login", wait_until="domcontentloaded")
        LOGGED_IN["wellfound"] = core.wait_human(
            page, lambda p: "/login" not in p.url and logged(p), "Wellfound'a giriş yap", minutes=10)
    return LOGGED_IN["wellfound"]


def wellfound_apply(page, job: dict, applied: dict | None = None) -> str:
    if not wellfound_login(page) or not goto(page, job["url"], "Wellfound"):
        return "blocked"
    ctx, before, start = page.context, len(page.context.pages), page.url
    btn = page.get_by_role("button", name=re.compile(r"^\s*apply( now)?\s*$", re.I)).first
    if not btn.count():
        return "already_applied_or_closed"
    btn.click()
    core.pause(3, 4)
    if len(ctx.pages) > before or not page.url.startswith("https://wellfound.com"):
        # Başvuru şirket sitesinde
        new = ctx.pages[-1] if len(ctx.pages) > before else page
        url = new.url
        if new is not page:
            new.close()
        else:
            page.goto(start, wait_until="domcontentloaded")
        return external(page, job, url, "wellfound.com", applied)
    form = page.locator(f"{core.MODAL}, form:has(textarea)").first
    if not form.count():
        core.screenshot(page, f"{job['id'].replace(':', '_')}_noform")
        return "skipped_no_form"
    unanswered = core.fill_step(page, form, external=True)
    if core.require_manual_review(page, form, unanswered, job["id"], job["title"], job["company"], job["url"]):
        return "manual_review"
    send = form.get_by_role("button", name=re.compile(r"send application|submit|^apply", re.I)).last
    if not send.count():
        core.screenshot(page, f"{job['id'].replace(':', '_')}_unanswered")
        return "skipped_unanswered"
    if core.DRY_RUN:
        core.screenshot(page, f"{job['id'].replace(':', '_')}_dryrun")
        page.keyboard.press("Escape")
        return "dry_run"
    before = core.application_submission_signals(page)
    expected_host = urlparse(page.url).hostname
    return core.submit_application(page, send, job["id"], job["title"], job["company"], job["url"],
        lambda: core.wait_application_submission(page, before, checks=15, expected_host=expected_host), applied)


def wellfound(page):
    seen = set()
    for kw in core.CFG["search"]["keywords"]:
        if not goto(page, f"https://wellfound.com/role/r/{slug(kw)}", "Wellfound"):
            return
        ids = dict.fromkeys(re.findall(r"/jobs/(\d+-[\w-]+)", page.content()))
        for jid in ids:
            if jid in seen:
                continue
            seen.add(jid)
            url = f"https://wellfound.com/jobs/{jid}"
            prev = core.load_applied().get(f"wellfound:{jid}")
            if prev and not core.can_retry(prev["status"]):
                continue
            if not goto(page, url, "Wellfound"):
                return
            # Sayfa başlığı: "Machine Learning Engineer at Noesia • San Francisco • Remote"
            m = re.match(r"(.+?) at (.+?)(?: •|$)", page.title())
            title, company = (m.group(1), m.group(2)) if m else (page.title(), "")
            job = {"id": f"wellfound:{jid}", "title": title, "company": company, "url": url,
                   "desc": page.locator("body").inner_text()[:20000], "location": ""}
            yield job, (lambda applied=None, job=job: wellfound_apply(page, job, applied))


# ---------------------------------------------------------------- Indeed

def indeed_apply(page, job: dict, applied: dict | None = None) -> str:
    if not goto(page, job["url"], "Indeed"):
        return "blocked"
    easy = page.locator("#indeedApplyButton, button[id*='indeedApply']").first
    if not easy.count():
        btn = page.get_by_role("button", name=re.compile(r"apply on company site", re.I)).or_(
            page.get_by_role("link", name=re.compile(r"apply on company site", re.I)))
        return external(page, job, core.follow_apply(page, btn.first), "indeed.com", applied)
    ctx, before = page.context, len(page.context.pages)
    easy.click()
    core.pause(4, 6)
    ap = ctx.pages[-1] if len(ctx.pages) > before else page
    for _ in range(15):
        core.pause(1.5, 3)
        if re.search(r"secure\.indeed\.com/(auth|account)", ap.url):
            if not core.wait_human(ap, lambda p: not re.search(r"secure\.indeed\.com/(auth|account)", p.url),
                                   "Indeed'e giriş yap", minutes=10):
                return "blocked"
            continue
        if not core.wait_human(ap, page_ready, "Indeed güvenlik doğrulamasını geç"):
            return "blocked"
        scope = ap.locator("main").first if ap.locator("main").count() else ap.locator("body")
        unanswered = core.fill_step(ap, scope, external=True)
        if core.require_manual_review(ap, scope, unanswered, job["id"], job["title"], job["company"], job["url"]):
            return "manual_review"
        primary = ap.get_by_role("button", name=re.compile(
            r"^(continue|review your application|submit your application|devam|başvurunu gönder)", re.I)).last
        if not primary.count():
            core.screenshot(ap, f"{job['id'].replace(':', '_')}_nobutton")
            return "skipped_no_button"
        text = primary.inner_text().lower()
        if "submit" in text or "gönder" in text:
            if core.DRY_RUN:
                core.screenshot(ap, f"{job['id'].replace(':', '_')}_dryrun")
                return "dry_run"
            before = core.application_submission_signals(ap)
            expected_host = urlparse(ap.url).hostname
            return core.submit_application(ap, primary, job["id"], job["title"], job["company"], job["url"],
                lambda: core.wait_application_submission(ap, before, checks=15, expected_host=expected_host), applied)
        before_url, before_text = ap.url, ap.locator("body").inner_text()[:3000]
        primary.click()
        core.pause(2, 3)
        if ap.url == before_url and ap.locator("body").inner_text()[:3000] == before_text:
            core.screenshot(ap, f"{job['id'].replace(':', '_')}_unanswered")
            return "skipped_unanswered"
    return "skipped_too_many_steps"


def indeed(page):
    cfg = core.CFG["sources"]
    domain = cfg.get("indeed_domain", "www.indeed.com")
    days = max(1, core.CFG["search"]["posted_within_hours"] // 24)
    seen = set()
    for kw in core.CFG["search"]["keywords"]:
        url = f"https://{domain}/jobs?q={quote_plus(kw)}&l=Remote&fromage={days}&sort=date"
        if not goto(page, url, "Indeed"):
            return
        cards = page.locator("[data-jk]").evaluate_all(
            "els => els.map(e => [e.getAttribute('data-jk'), (e.closest('li, .job_seen_beacon, td') || e).innerText])")
        for jk, text in cards:
            if jk in seen:
                continue
            seen.add(jk)
            lines = [l.strip() for l in text.splitlines() if l.strip()]
            title = re.sub(r"^new\s+", "", lines[0], flags=re.I) if lines else ""
            company = lines[1] if len(lines) > 1 else ""
            if cfg.get("indeed_easy_apply_only", False) and not re.search(r"easily apply", text, re.I):
                continue
            job = {"id": f"indeed:{jk}", "title": title, "company": company,
                   "url": f"https://{domain}/viewjob?jk={jk}", "desc": text, "location": ""}
            yield job, (lambda applied=None, job=job: indeed_apply(page, job, applied))


# ---------------------------------------------------------------- ortak döngü

SOURCES = {
    "wellfound": wellfound,
    "indeed": indeed,
    "remoteok": remoteok,
    "himalayas": himalayas,
    "weworkremotely": weworkremotely,
}


def run(apply_module, page, applied: dict, count: int) -> int:
    global core
    core = apply_module
    limits = core.CFG["limits"]
    for name, source in SOURCES.items():
        if not core.CFG["sources"].get(name):
            continue
        if count >= limits["max_applications_per_run"] or time.time() > core.DEADLINE:
            break
        core.log(f"=== {name} ===")
        done = 0
        try:
            for job, apply in source(page):
                if count >= limits["max_applications_per_run"] or time.time() > core.DEADLINE:
                    core.log("Günlük limit/süre doldu.")
                    return count
                if done >= limits.get("per_source", 15):
                    break
                prev = applied.get(job["id"])
                if prev and not core.can_retry(prev["status"]):
                    continue
                reason = (core.job_ok(job["title"], job["company"], job["desc"])
                          or ("applied_elsewhere" if core.applied_elsewhere(applied, job["title"], job["company"]) else None))
                if reason:
                    core.log(f"- atla [{reason}] {job['title']} @ {job['company']}")
                    core.record(applied, job["id"], job["title"], job["company"], reason, job["url"])
                    continue
                metadata = job.get("metadata") or {}
                # Resolve unknown metadata from this job's detail page, not search cards.
                preliminary = job_metadata.eligibility_reason(metadata, core.CFG["search"]["posted_within_hours"],
                                                               core.CFG["search"].get("remote_only", False))
                if preliminary in ("posting_date_unknown", "posting_date_future", "workplace_unknown"):
                    if page.url == job["url"] or goto(page, job["url"], name):
                        metadata = job_metadata.merge_metadata(metadata, core.read_job_metadata(page, name, job["url"]))
                job["metadata"] = metadata
                if not core.check_job_eligibility(job, applied):
                    continue
                core.log(f"> {job['title']} @ {job['company']} ({name})")
                try:
                    status = apply(applied)
                except Exception as e:
                    if core.browser_closed(e):
                        raise core.BrowserClosed() from None
                    core.log(f"  ! hata: {e.__class__.__name__}: {str(e)[:150]}")
                    core.screenshot(page, f"{job['id'].replace(':', '_')}_error")
                    # A durable submit state must survive exceptions in any caller.
                    status = applied.get(job["id"], {}).get("status")
                    if status not in core.UNCERTAIN_STATUSES and status not in ("applied", "applied_external", "submission_rejected"):
                        status = "pre_submission_error"
                core.log(f"  = {status}")
                if status != "dry_run" and applied.get(job["id"], {}).get("status") != status:
                    core.record(applied, job["id"], job["title"], job["company"], status, job["url"])
                if status in ("applied", "applied_external", "dry_run"):
                    count += 1
                    done += 1
                if status == "blocked":
                    core.log(f"  {name} erişimi engelledi, sonraki kaynağa geçiliyor.")
                    break
                core.pause(4, 9)
        except Exception as e:
            if core.browser_closed(e):
                raise core.BrowserClosed() from None
            core.log(f"  ! {name} okunamadı: {e.__class__.__name__}: {str(e)[:150]}")
    return count
