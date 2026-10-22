"""Job sources other than LinkedIn.

Wellfound and Indeed use their own application flows and may require manual login.
Remote OK, Himalayas and We Work Remotely collect jobs through APIs/RSS and link
to company forms. Approved ATS forms use apply.apply_form with quotas/cooldowns;
unsupported forms are recorded in data/manual_apply.csv.

Each source yields jobs; the shared loop filters, deduplicates across sources
using company/title keys, and records outcomes.
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
core = None  # The apply module, assigned by run().


# ---------------------------------------------------------------- helpers

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
    """On queryless sources, require all meaningful words of a configured job title."""
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
    if what.casefold() == 'indeed':
        return indeed_wait_ready(page, 'search' if urlparse(url).path.rstrip('/') == '/jobs' else 'detail')
    return core.wait_human(page, page_ready, f"complete the {what} security verification")


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
            "sign in to We Work Remotely (a free account is required to view application links)")
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
            page, lambda p: "/login" not in p.url and logged(p), "sign in to Wellfound", minutes=10)
    return LOGGED_IN["wellfound"]


WELLFOUND_OPENER_JS = r"""job => {
    document.querySelectorAll('[data-autoapply-wellfound-opener]').forEach(e => e.removeAttribute('data-autoapply-wellfound-opener'));
    if (location.origin + location.pathname.replace(/\/$/, '') !== job.url.replace(/\/$/, '')) return false;
    const clean = s => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
    const visible = e => e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden';
    const headings = [...document.querySelectorAll('h1')].filter(e => visible(e) && clean(e.innerText) === clean(job.title));
    if (headings.length !== 1) return false;
    for (let root = headings[0].parentElement, depth = 0; root && depth < 6; root = root.parentElement, depth++) {
        if (root.matches('body, html') || root.querySelectorAll('h1').length !== 1) break;
        const otherJob = [...root.querySelectorAll('a[href*="/jobs/"]')].some(e => {
            const u = new URL(e.href, location.href);
            return /\/jobs\/\d+/.test(u.pathname) && u.origin + u.pathname.replace(/\/$/, '') !== job.url.replace(/\/$/, '');
        });
        if (otherJob) break;
        const buttons = [...root.querySelectorAll('button')].filter(e => visible(e) && /^apply( now)?$/i.test(clean(e.innerText))
            && (!e.getAttribute('aria-label') || /^apply( now)?$/i.test(clean(e.getAttribute('aria-label')))));
        if (buttons.length === 1) { buttons[0].setAttribute('data-autoapply-wellfound-opener', 'true'); return true; }
        if (buttons.length > 1) return false;
    }
    return false;
}"""


def wellfound_apply(page, job: dict, applied: dict | None = None) -> str:
    if not wellfound_login(page) or not goto(page, job["url"], "Wellfound"):
        return "blocked"
    ctx, before, start = page.context, len(page.context.pages), page.url
    if page.evaluate(WELLFOUND_OPENER_JS, {'url': job['url'], 'title': job['title']}) is not True:
        core.record_manual(job['id'], job['title'], job['company'], job['url'], 'Cannot uniquely associate Apply with the current job')
        return 'manual_review'
    btn = page.locator('[data-autoapply-wellfound-opener="true"]')
    try:
        core.click_form_control(btn)
    except core.UnsafeFormControl:
        core.record_manual(job["id"], job["title"], job["company"], job["url"], "submit-capable Apply opener")
        return "manual_review"
    core.pause(3, 4)
    if len(ctx.pages) > before or not page.url.startswith("https://wellfound.com"):
        # The application continues on a company site.
        new = ctx.pages[-1] if len(ctx.pages) > before else page
        url = new.url
        if new is not page:
            new.close()
        else:
            page.goto(start, wait_until="domcontentloaded")
        return external(page, job, url, "wellfound.com", applied)
    forms = page.locator("dialog[open]:visible, div[role='dialog']:visible")
    if not forms.count():
        forms = page.locator('form:has(textarea):visible')
    if forms.count() != 1:
        core.record_manual(job['id'], job['title'], job['company'], job['url'], 'Application form missing or ambiguous after Apply')
        return 'manual_review'
    form = forms.first
    unanswered = core.fill_step(page, form, external=True)
    if core.require_manual_review(page, form, unanswered, job["id"], job["title"], job["company"], job["url"], external=True):
        return "manual_review"
    sends = form.get_by_role("button", name=re.compile(r"send application|submit|^apply", re.I))
    if not sends.count():
        core.screenshot(page, f"{job['id'].replace(':', '_')}_unanswered")
        return "skipped_unanswered"
    if sends.count() != 1:
        core.record_manual(job['id'], job['title'], job['company'], job['url'], 'Ambiguous final application action')
        return 'manual_review'
    send = sends.first
    if core.application_button_action(send) != 'submit':
        core.record_manual(job['id'], job['title'], job['company'], job['url'], 'Unrecognized final application action')
        return 'manual_review'
    if core.DRY_RUN:
        core.screenshot(page, f"{job['id'].replace(':', '_')}_dryrun")
        page.keyboard.press("Escape")
        return core.preview_application(job["id"])
    before = core.application_submission_signals(page)
    expected_host = urlparse(page.url).hostname
    return core.submit_application(page, send, job["id"], job["title"], job["company"], job["url"],
        lambda: core.wait_application_submission(page, before, checks=15, expected_host=expected_host), applied)


WELLFOUND_SEARCH_JS = r"""() => {
    const input = document.querySelector('input[id^="react-select-"]');
    const control = input?.closest('.select__control');
    const value = control?.querySelector('.select__single-value');
    return value && value.getClientRects().length ? value.innerText.trim() : '';
}"""


def wellfound_search_verified(page, requested_url: str, keyword: str) -> bool:
    """Never treat a generic /remote redirect or an unselected role as a query."""
    expected, actual = urlparse(requested_url), urlparse(page.url)
    if (actual.scheme, actual.netloc, actual.path.rstrip('/')) != (expected.scheme, expected.netloc, expected.path.rstrip('/')):
        return False
    for attempt in range(3):
        selected = page.evaluate(WELLFOUND_SEARCH_JS)
        if isinstance(selected, str) and ' '.join(selected.split()).casefold() == ' '.join(keyword.split()).casefold():
            return True
        if attempt < 2:
            page.wait_for_timeout(500)
    return False


def _wellfound_public(page):
    seen = set()
    for kw in core.CFG["search"]["keywords"]:
        search_url = f"https://wellfound.com/role/r/{slug(kw)}"
        if not goto(page, search_url, "Wellfound"):
            core.METRICS.mark('searches_failed', f'wellfound:{kw}')
            return
        if not wellfound_search_verified(page, search_url, kw):
            core.METRICS.mark('searches_failed', f'wellfound:{kw}')
            core.log(f"  ! Wellfound search not applied for {kw!r}: requested {search_url}, actual {page.url.split('?')[0]}. "
                     'The exact role was not confirmed; generic results will not be processed. Choose a supported role explicitly in the GUI; no replacement was made.')
            continue
        core.METRICS.mark('searches_verified', f'wellfound:{kw}')
        ids = dict.fromkeys(re.findall(r"/jobs/(\d+-[\w-]+)", page.content()))
        for jid in ids:
            if jid in seen:
                continue
            seen.add(jid)
            url = f"https://wellfound.com/jobs/{jid}"
            prev = core.load_applied().get(f"wellfound:{jid}")
            if prev and not core.can_retry(prev["status"]) and prev['status'] not in core.job_rechecks.RECHECKABLE:
                core.METRICS.mark('seen', f'wellfound:{jid}')
                core.METRICS.mark('history_skipped', f'wellfound:{jid}')
                continue
            if not goto(page, url, "Wellfound"):
                return
            # Page title: "Machine Learning Engineer at Noesia • San Francisco • Remote".
            m = re.match(r"(.+?) at (.+?)(?: •|$)", page.title())
            title, company = (m.group(1), m.group(2)) if m else (page.title(), "")
            metadata = core.read_job_metadata(page, 'wellfound', url)
            description = metadata.get('description') or ''
            if description:
                description = html.unescape(re.sub(r'<[^>]+>', '\n', description))
            else:
                scoped = page.locator('[data-testid="job-description"], [data-test="job-description"], #job-description, .job-description')
                description = scoped.first.inner_text() if scoped.count() else ''
            job = {"id": f"wellfound:{jid}", "title": title, "company": company, "url": url,
                   "desc": description, "location": "", 'metadata': metadata}
            yield job, (lambda applied=None, job=job: wellfound_apply(page, job, applied))


def wellfound(page):
    """Use native literal keyword search when signed in; preserve strict public fallback."""
    from wellfound_search import NativeSearch, SearchUnavailable
    search_page = page.context.new_page()
    search_page.set_default_timeout(6000)
    search = NativeSearch(search_page, core)
    try:
        try:
            search.start()
        except SearchUnavailable as e:
            core.log(f'  ! Wellfound native keyword search unavailable: {e}. Trying only verifiable exact public roles.')
            yield from _wellfound_public(page)
            return
        seen = set()
        for keyword in list(core.CFG['search']['keywords']):
            if core.STOP.is_set():
                raise core.Stopped
            if time.time() > core.DEADLINE:
                return
            try:
                jobs, more = search.search(keyword)
            except SearchUnavailable as e:
                core.METRICS.mark('searches_failed', f'wellfound:{keyword}')
                core.log(f'  ! Wellfound native search failed for {keyword!r}: {e}')
                continue
            core.METRICS.mark('searches_verified', f'wellfound:{keyword}')
            # A previously failed login must not remain cached after the
            # authenticated native search has demonstrably succeeded.
            LOGGED_IN['wellfound'] = True
            core.log(f'  Wellfound native keyword {keyword!r}: {len(jobs)} job listings. '
                     f'Site company-region scope: {search.company_region_label or "not specified"}. '
                     'GUI regions are not mapped here; Wellfound may additionally limit results by account location.')
            if more:
                core.log('  ! Wellfound has more native results; only the first page is currently supported.')
            for job in jobs:
                if job['id'] in seen:
                    continue
                seen.add(job['id'])
                core.METRICS.mark('seen', job['id'])
                history = core.load_applied()
                identity_prefix = job['id'].split('-', 1)[0]
                previous = [value for key, value in history.items()
                            if key == identity_prefix or key.startswith(identity_prefix + '-')]
                protected = any(not core.can_retry(value['status'])
                                and value['status'] not in core.job_rechecks.RECHECKABLE for value in previous)
                if job.pop('provider_applied') or protected:
                    core.METRICS.mark('history_skipped', job['id'])
                    continue
                workplace = job.pop('native_workplace')
                job['metadata'] = {'posted_at': job.pop('native_posted_at'),
                                   'workplace': job_metadata.workplace_kind(workplace.replace('_', ' ') if isinstance(workplace, str) else None,
                                                                           job.pop('native_remote')),
                                   'location': job['location'],
                                   'diagnostic': {'source': 'wellfound_native_results',
                                                  'keyword': keyword, 'page': 1,
                                                  'company_region_scope': search.company_region_label,
                                                  'gui_regions_applied': False}}
                yield job, (lambda applied=None, job=job: wellfound_apply(page, job, applied))
    finally:
        search.close()


# ---------------------------------------------------------------- Indeed

INDEED_AUTH_PATH = re.compile(r'/(?:auth|account|accounts|login|signin|sign-in|registration)(?:/|$)', re.I)
INDEED_AUTH_HEADING = re.compile(r'^\s*(?:sign\s*in|log\s*in|create (?:an? |your )?account|welcome back|verify (?:your )?email|enter (?:your )?(?:email|password|code)|two.step verification)\b', re.I)


def indeed_host(page) -> bool:
    host = (urlparse(page.url).hostname or '').lower()
    configured = core.CFG['sources'].get('indeed_domain', 'www.indeed.com').lower().removeprefix('www.')
    return bool(host) and (host == configured or host.endswith('.' + configured)
                           or host == 'indeed.com' or host.endswith('.indeed.com'))


def indeed_auth_pending(page) -> bool:
    parsed = urlparse(page.url)
    host = (parsed.hostname or '').lower()
    if host in ('accounts.indeed.com', 'account.indeed.com', 'auth.indeed.com') or INDEED_AUTH_PATH.search(parsed.path):
        return True
    if page.locator('input[type="password"]:visible, input[autocomplete="username"]:visible, form[action*="/login"]:visible').count():
        return True
    return bool(page.get_by_role('heading', name=INDEED_AUTH_HEADING).count())


def indeed_ready_evidence(page, stage: str) -> bool:
    """Positive page evidence, not just the temporary absence of a challenge."""
    try:
        if not indeed_host(page) or core.blocked(page) or indeed_auth_pending(page):
            return False
        if stage == 'search':
            return bool(page.locator('[data-jk]:visible, .jobsearch-NoResult-messageContainer:visible, [data-testid="no-results"]:visible, #no_results:visible').count())
        if stage == 'detail':
            return bool(page.locator('#jobDescriptionText:visible, [data-testid="jobsearch-JobInfoHeader-title"]:visible, .jobsearch-JobInfoHeader-title:visible, #indeedApplyButton:visible').count())
        if stage == 'application':
            return bool(page.get_by_role('button', name=core.NAVIGATION_BUTTON_RE).count()
                        or page.get_by_role('button', name=core.FINAL_BUTTON_RE).count())
        return False
    except Exception as e:
        if core.browser_closed(e):
            raise core.BrowserClosed() from None
        # Redirects may briefly destroy the document. They are never readiness.
        return False


def indeed_wait_ready(page, stage: str) -> bool:
    """Wait passively through login/MFA/challenge; never reload or navigate here."""
    state = {'url': None, 'since': None}
    def stable(p):
        if core.STOP.is_set():
            raise core.Stopped
        if not indeed_ready_evidence(p, stage):
            state.update(url=None, since=None)
            return False
        now = time.monotonic()
        if state['url'] != p.url or state['since'] is None:
            state.update(url=p.url, since=now)
            return False
        return now - state['since'] >= 1
    # Avoid asking an already-ready user to sign in just for the stability check.
    if indeed_ready_evidence(page, stage):
        stable(page)
        page.wait_for_timeout(1100)
        if stable(page):
            return True
    return core.wait_human(page, stable,
                           'finish Indeed sign-in or security verification (the app will not reload the page)', minutes=10)


def indeed_apply(page, job: dict, applied: dict | None = None) -> str:
    if not goto(page, job["url"], "Indeed"):
        return "blocked"
    easy = page.locator("#indeedApplyButton, button[id*='indeedApply']").first
    if not easy.count():
        btn = page.get_by_role("button", name=re.compile(r"apply on company site", re.I)).or_(
            page.get_by_role("link", name=re.compile(r"apply on company site", re.I)))
        return external(page, job, core.follow_apply(page, btn.first), "indeed.com", applied)
    ctx, before = page.context, len(page.context.pages)
    try:
        core.click_form_control(easy)
    except core.UnsafeFormControl:
        core.record_manual(job["id"], job["title"], job["company"], job["url"], "submit-capable Indeed opener")
        return "manual_review"
    core.pause(4, 6)
    ap = ctx.pages[-1] if len(ctx.pages) > before else page
    for _ in range(15):
        core.pause(1.5, 3)
        if not indeed_wait_ready(ap, 'application'):
            return "blocked"
        scope = ap.locator("main").first if ap.locator("main").count() else ap.locator("body")
        unanswered = core.fill_step(ap, scope, external=True)
        if core.require_manual_review(ap, scope, unanswered, job["id"], job["title"], job["company"], job["url"], external=True):
            return "manual_review"
        primary = ap.get_by_role("button", name=re.compile(
            r"^(next|continue|review|submit|send(?: your)? application|devam|[iİıI]leri|başvurunu gönder|gönder)", re.I)).last
        if not primary.count():
            core.screenshot(ap, f"{job['id'].replace(':', '_')}_nobutton")
            return "skipped_no_button"
        action = core.application_button_action(primary)
        if action == "submit":
            if core.DRY_RUN:
                core.screenshot(ap, f"{job['id'].replace(':', '_')}_dryrun")
                return core.preview_application(job["id"])
            before = core.application_submission_signals(ap)
            expected_host = urlparse(ap.url).hostname
            return core.submit_application(ap, primary, job["id"], job["title"], job["company"], job["url"],
                lambda: core.wait_application_submission(ap, before, checks=15, expected_host=expected_host), applied)
        if action != "next":
            core.require_manual_review(ap, scope, ["unrecognized application action"], job["id"], job["title"], job["company"], job["url"])
            return "manual_review"
        before_url, before_text = ap.url, ap.locator("body").inner_text()[:3000]
        try:
            core.click_navigation(primary)
        except core.UnsafeFormControl:
            core.require_manual_review(ap, scope, ["submit-capable navigation control"], job["id"], job["title"], job["company"], job["url"])
            return "manual_review"
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


# ---------------------------------------------------------------- shared source loop

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
        if count >= limits["max_applications_per_run"] or core.submission_quota_status() or time.time() > core.DEADLINE:
            break
        if core.submission_quota_status(name):
            continue
        core.log(f"=== {name} ===")
        done = 0
        try:
            for job, apply in source(page):
                if count >= limits["max_applications_per_run"] or core.submission_quota_status() or time.time() > core.DEADLINE:
                    core.log("Run application limit or runtime limit reached.")
                    return count
                if done >= limits.get("per_source", 15) or core.submission_quota_status(name):
                    break
                if not core.should_visit_job(applied, job['id']):
                    continue
                core.METRICS.mark('evaluated', job['id'])
                reason = (core.job_ok(job["title"], job["company"], job["desc"])
                          or ("applied_elsewhere" if core.applied_elsewhere(applied, job["title"], job["company"]) else None))
                if reason:
                    core.log(f"- skip [{reason}] {job['title']} @ {job['company']}")
                    core.record_relevance_decision(applied, job['id'], job['title'], job['company'], reason, job['url'], job['desc'])
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
                core.mark_evaluation_passed(applied, job['id'], job['title'], job['company'], job['url'])
                try:
                    with core.submission_source(name), core.METRICS.application(job['id']):
                        status = apply(applied)
                except Exception as e:
                    if core.browser_closed(e):
                        raise core.BrowserClosed() from None
                    core.log(f"  ! error: {e.__class__.__name__}: {str(e)[:150]}")
                    core.screenshot(page, f"{job['id'].replace(':', '_')}_error")
                    # A durable submit state must survive exceptions in any caller.
                    status = applied.get(job["id"], {}).get("status")
                    if status not in core.UNCERTAIN_STATUSES and status not in ("applied", "applied_external", "submission_rejected"):
                        status = "pre_submission_error"
                core.log(f"  = {status}")
                core.METRICS.outcome(job['id'], status)
                if status != "dry_run" and applied.get(job["id"], {}).get("status") != status:
                    core.record(applied, job["id"], job["title"], job["company"], status, job["url"])
                if status in ("applied", "applied_external", "dry_run"):
                    count += 1
                    done += 1
                if status == "blocked":
                    core.log(f"  Access to {name} was blocked; moving to the next source.")
                    break
                core.pause(4, 9)
        except Exception as e:
            if core.browser_closed(e):
                raise core.BrowserClosed() from None
            core.log(f"  ! could not read {name}: {e.__class__.__name__}: {str(e)[:150]}")
    return count
