"""Conservative, shared posting-date and workplace checks for every job source."""
import datetime as dt
from email.utils import parsedate_to_datetime
import math
import re
from urllib.parse import parse_qsl, urlparse

UTC = dt.timezone.utc


def posting_time(value, now: dt.datetime | None = None) -> dt.datetime | None:
    now = now or dt.datetime.now(UTC)
    try:
        if isinstance(value, dt.datetime):
            return value.astimezone(UTC) if value.tzinfo else None
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if not math.isfinite(value):
                return None
            return dt.datetime.fromtimestamp(value / 1000 if value > 100_000_000_000 else value, UTC)
        if not isinstance(value, str) or not value.strip():
            return None
        text = value.strip()
        if re.fullmatch(r"\d{10}|\d{13}", text):
            return posting_time(int(text), now)
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            # A date without a timezone may be UTC+14: use its oldest possible instant.
            return dt.datetime.fromisoformat(text).replace(tzinfo=UTC) - dt.timedelta(hours=14)
        relative = re.search(r"\b(\d+)\+?\s*(minute|hour|day|week|month|year)s?\s+ago\b", text, re.I)
        if relative:
            units = {"minute": 60, "hour": 3600, "day": 86400, "week": 604800,
                     "month": 31 * 86400, "year": 366 * 86400}
            # Rounded labels are intervals: '7 days ago' does not prove <= 7 days.
            if "+" in relative.group(0):
                return None
            return now - dt.timedelta(seconds=(int(relative[1]) + 1) * units[relative[2].lower()])
        if re.fullmatch(r"(?:posted\s+)?(?:just now|today)", text, re.I):
            return now - dt.timedelta(days=1)
        if re.fullmatch(r"(?:posted\s+)?yesterday", text, re.I):
            return now - dt.timedelta(days=2)
        try:
            parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            parsed = parsedate_to_datetime(text)
        return parsed.astimezone(UTC) if parsed.tzinfo else None
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def workplace_kind(*values) -> str | None:
    kinds = set()
    for value in values:
        if isinstance(value, (list, tuple)):
            kind = workplace_kind(*value)
            if kind:
                kinds.add(kind)
            continue
        if isinstance(value, bool):
            kinds.add("remote" if value else "onsite")
            continue
        if not isinstance(value, str):
            continue
        for line in value.splitlines():
            line = line.strip().casefold()
            if re.match(r"^(hybrid|hibrit)(?:$|\s*[-(:,|])", line):
                kinds.add("hybrid")
            elif re.match(r"^(on[- ]?site|onsite|office[- ]based|not remote)(?:$|\s*[-(:,|])", line):
                kinds.add("onsite")
            elif re.match(r"^(remote|telecommute|fully remote|100% remote|work from home|uzaktan)(?:$|\s*[-(:,|])", line):
                kinds.add("remote")
    # Conflicting evidence must never override an office/hybrid restriction.
    return next((k for k in ("hybrid", "onsite", "remote") if k in kinds), None)


def eligibility_reason(metadata: dict, hours: float, remote_only: bool = True,
                       now: dt.datetime | None = None) -> str | None:
    now = now or dt.datetime.now(UTC)
    workplace = workplace_kind(metadata.get("workplace"))
    if remote_only and workplace in ("onsite", "hybrid"):
        return "not_remote"
    posted = posting_time(metadata.get("posted_at"), now)
    if posted is not None:
        age = (now - posted).total_seconds()
        if age < 0:
            return "posting_date_future"
        if age > hours * 3600:
            return "posting_too_old"
    else:
        return "posting_date_unknown"
    if remote_only and workplace != "remote":
        return "workplace_unknown"
    return None


def merge_metadata(first: dict, second: dict) -> dict:
    times = [p for m in (first, second) if (p := posting_time(m.get("posted_at"))) is not None]
    return {"posted_at": min(times) if times else None,
            "workplace": workplace_kind(first.get("workplace"), second.get("workplace")),
            "location": first.get("location") or second.get("location") or ""}


METADATA_JS = r"""source => {
    const postings = [];
    function visit(value) {
        if (Array.isArray(value)) return value.forEach(visit);
        if (!value || typeof value !== 'object') return;
        const types = [].concat(value['@type'] || []);
        if (types.some(t => /(^|[/#])JobPosting$/.test(t))) postings.push(value);
        if (value['@graph']) visit(value['@graph']);
        if (value.mainEntity) visit(value.mainEntity);
    }
    for (const el of document.querySelectorAll('script[type="application/ld+json"]')) {
        try { visit(JSON.parse(el.textContent)); } catch (_) {}
    }
    // Only job-detail metadata areas; never search the description or recommendations.
    const selectors = {
        linkedin: '.job-details-jobs-unified-top-card, .jobs-unified-top-card, .job-details-fit-level-preferences',
        wellfound: '[data-test="job-header"], [data-testid="job-header"], [data-test="job-detail-header"]',
        indeed: '#jobDetailsSection, #jobLocationText, .jobsearch-JobMetadataFooter, [data-testid="job-age"]'
    };
    const roots = [...document.querySelectorAll(selectors[source] || '[data-autoapply-job-metadata]')]
        .filter(e => e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden');
    return {postings, title: document.querySelector('h1')?.innerText || '', labels: roots.map(e => e.innerText),
        times: roots.flatMap(e => [...e.querySelectorAll('time[datetime]')].map(t => t.dateTime))};
}"""


def page_metadata(page, source: str, expected_url: str) -> dict:
    raw = page.evaluate(METADATA_JS, source)
    def identity(url):
        parsed = urlparse(url)
        path = parsed.path.rstrip("/")
        if source == "linkedin":
            match = re.search(r"/jobs/view/(?:[^/]*-)?(\d+)$", path)
            if match:
                return parsed.hostname, "/jobs/view/" + match[1], ()
        query = tuple(sorted((k, v) for k, v in parse_qsl(parsed.query) if not k.startswith("utm_")))
        return parsed.hostname, path, query
    expected = identity(expected_url)
    if identity(page.url) != expected:
        return {}  # Login/redirect pages are not evidence about the requested job.
    matches, anonymous = [], []
    for posting in raw.get("postings", []):
        urls = [posting.get("url"), posting.get("@id"), posting.get("mainEntityOfPage")]
        urls = [u.get("@id") if isinstance(u, dict) else u for u in urls]
        urls = [u for u in urls if isinstance(u, str) and u.startswith("https://")]
        if any(identity(u) == expected for u in urls):
            matches.append(posting)
        elif not urls:
            anonymous.append(posting)
    posting = matches[0] if len(matches) == 1 else {}
    if not matches and len(raw.get("postings", [])) == 1 and len(anonymous) == 1:
        candidate = anonymous[0]
        title = raw.get("title", "").strip().casefold()
        if title and str(candidate.get("title", "")).strip().casefold() == title:
            posting = candidate
    labels = raw.get("labels", [])
    dates = [posting.get("datePosted"), *raw.get("times", []), *labels]
    times = [p for v in dates if (p := posting_time(v)) is not None]
    posted = min(times) if times else None
    restrictions = posting.get("applicantLocationRequirements") or []
    restrictions = restrictions if isinstance(restrictions, list) else [restrictions]
    location = ", ".join(x.get("name", "") for x in restrictions if isinstance(x, dict))
    return {"posted_at": posted,
            "workplace": workplace_kind(posting.get("jobLocationType"), *labels), "location": location}
