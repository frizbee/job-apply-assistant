"""Job sources other than LinkedIn.

Wellfound and Indeed use their own application flows and may require manual login.
Remote OK, Himalayas and We Work Remotely collect jobs through APIs/RSS and link
to company forms. Approved ATS forms use apply.apply_form with quotas/cooldowns;
unsupported forms are recorded in data/manual_apply.csv.

Each source yields jobs; the shared loop filters, deduplicates across sources
using company/title keys, and records outcomes.
"""

import sys
from platforms.remoteok import RemoteOKAdapter
from platforms.himalayas import HimalayasAdapter
from platforms.weworkremotely import WeWorkRemotelyAdapter
from platforms.wellfound import WellfoundAdapter
from platforms.indeed import IndeedAdapter
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
    if what.casefold() == 'indeed' and urlparse(url).path.rstrip('/') == '/jobs':
        return IndeedAdapter(sys.modules[__name__]).indeed_open_search(page, url)
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
    return RemoteOKAdapter(sys.modules[__name__]).remoteok(page)


def remoteok_details(page, job):
    return RemoteOKAdapter(sys.modules[__name__]).prepare_job(page, job)


# ---------------------------------------------------------------- Himalayas

def himalayas(page):
    return HimalayasAdapter(sys.modules[__name__]).himalayas(page)


# ---------------------------------------------------------------- We Work Remotely

def wwr_login(page) -> bool:
    return WeWorkRemotelyAdapter(sys.modules[__name__]).wwr_login(page)


def weworkremotely(page):
    return WeWorkRemotelyAdapter(sys.modules[__name__]).weworkremotely(page)


# ---------------------------------------------------------------- Wellfound

def wellfound_login(page) -> bool:
    return WellfoundAdapter(sys.modules[__name__]).wellfound_login(page)


from platforms.wellfound import WELLFOUND_OPENER_JS


def wellfound_apply(page, job: dict, applied: dict | None=None) -> str:
    return WellfoundAdapter(sys.modules[__name__]).wellfound_apply(page, job, applied)


from platforms.wellfound import WELLFOUND_SEARCH_JS


def wellfound_search_verified(page, requested_url: str, keyword: str) -> bool:
    return WellfoundAdapter(sys.modules[__name__]).wellfound_search_verified(page, requested_url, keyword)


def _wellfound_public(page):
    return WellfoundAdapter(sys.modules[__name__])._wellfound_public(page)


def wellfound(page):
    return WellfoundAdapter(sys.modules[__name__]).wellfound(page)


# ---------------------------------------------------------------- Indeed

from platforms.indeed import INDEED_AUTH_PATH
from platforms.indeed import INDEED_AUTH_HEADING


def indeed_host(page) -> bool:
    return IndeedAdapter(sys.modules[__name__]).indeed_host(page)


def indeed_auth_pending(page) -> bool:
    return IndeedAdapter(sys.modules[__name__]).indeed_auth_pending(page)


def indeed_ready_evidence(page, stage: str) -> bool:
    return IndeedAdapter(sys.modules[__name__]).indeed_ready_evidence(page, stage)


def indeed_wait_ready(page, stage: str, *, expected_url=None, search_state=None) -> bool:
    return IndeedAdapter(sys.modules[__name__]).indeed_wait_ready(page, stage, expected_url=expected_url, search_state=search_state)


def indeed_apply(page, job: dict, applied: dict | None=None) -> str:
    return IndeedAdapter(sys.modules[__name__]).indeed_apply(page, job, applied)


def indeed(page):
    return IndeedAdapter(sys.modules[__name__]).indeed(page)


# ---------------------------------------------------------------- shared source loop

SOURCES = {
    "wellfound": wellfound,
    "indeed": indeed,
    "remoteok": remoteok,
    "himalayas": himalayas,
    "weworkremotely": weworkremotely,
}

DETAIL_LOADERS = {'remoteok': remoteok_details}


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
                loader = DETAIL_LOADERS.get(name)
                if loader:
                    preliminary = job_metadata.eligibility_reason(job.get('metadata') or {}, core.CFG['search']['posted_within_hours'],
                                                                  core.CFG['search'].get('remote_only', False))
                    if preliminary in ('posting_too_old', 'not_remote'):
                        core.check_job_eligibility(job, applied)
                        continue
                    loader(page, job)
                    if job.get('detail_issue'):
                        core.check_job_eligibility(job, applied)
                        if job.get('detail_source_blocked'):
                            core.log(f'  Access to {name} was blocked; moving to the next source.')
                            break
                        continue
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
                if not loader and preliminary in ("posting_date_unknown", "posting_date_future", "workplace_unknown"):
                    ready = not core.blocked(page) if page.url == job["url"] else goto(page, job["url"], name)
                    if not ready:
                        core.check_job_eligibility(job, applied)
                        core.log(f'  Access to {name} was blocked during metadata inspection; moving to the next source without opening more jobs.')
                        break
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
