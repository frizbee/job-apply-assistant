"""Conservative, shared posting-date and workplace checks for every job source."""
import datetime as dt
from email.utils import parsedate_to_datetime
import math
import re
from urllib.parse import parse_qsl, urlparse, urljoin
from platforms.remoteok import REMOTEOK_ROOT_JS

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
        localized = [
            (r'hace\s+(\d+)(\+)?\s*(minutos?|horas?|d[ií]as?|semanas?|mes(?:es)?|a[nñ]os?)',
             {'minuto': 'minute', 'minutos': 'minute', 'hora': 'hour', 'horas': 'hour', 'día': 'day', 'días': 'day', 'dia': 'day', 'dias': 'day', 'semana': 'week', 'semanas': 'week', 'mes': 'month', 'meses': 'month', 'año': 'year', 'años': 'year'}),
            (r'h[áa]\s+(\d+)(\+)?\s*(minutos?|horas?|dias?|semanas?|m[eê]s(?:es)?|anos?)',
             {'minuto': 'minute', 'minutos': 'minute', 'hora': 'hour', 'horas': 'hour', 'dia': 'day', 'dias': 'day', 'semana': 'week', 'semanas': 'week', 'mês': 'month', 'meses': 'month', 'ano': 'year', 'anos': 'year'}),
            (r'(\d+)(\+)?\s*(dakika|saat|gün|hafta|ay|yıl)\s+önce',
             {'dakika': 'minute', 'saat': 'hour', 'gün': 'day', 'hafta': 'week', 'ay': 'month', 'yıl': 'year'}),
            (r'(\d+)(\+)?\s*(минут[аыу]?|час(?:а|ов)?|дн(?:я|ей)|день|недел[юьи]|месяц(?:а|ев)?|лет|год(?:а)?)\s+назад',
             {'минута': 'minute', 'минут': 'minute', 'минуты': 'minute', 'час': 'hour', 'часа': 'hour', 'часов': 'hour', 'день': 'day', 'дня': 'day', 'дней': 'day', 'неделю': 'week', 'недели': 'week', 'недель': 'week', 'месяц': 'month', 'месяца': 'month', 'месяцев': 'month', 'год': 'year', 'года': 'year', 'лет': 'year'}),
        ]
        for pattern, units in localized:
            match = re.search(pattern, text, re.I)
            if match:
                if match[2]:
                    return None
                unit = units.get(match[3].casefold())
                return posting_time(f'{match[1]} {unit}s ago', now) if unit else None
        if re.fullmatch(r'(?:hoy|hoje|bugün|сегодня)', text, re.I):
            return now - dt.timedelta(days=1)
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
            elif re.match(r"^(remote|remoto|en remoto|a distancia|telecommute|fully remote|100% remote|work from home|uzaktan|удал[её]нно)(?:$|\s*[-(:,|])", line):
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
            "location": first.get("location") or second.get("location") or "",
            'diagnostic': second.get('diagnostic') or first.get('diagnostic')}


LINKEDIN_ROOTS_JS = r"""() => {
    const visible = e => e && e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden';
    const roots = [...document.querySelectorAll('.job-details-jobs-unified-top-card, .jobs-unified-top-card, .job-details-fit-level-preferences, .top-card-layout, .topcard')].filter(visible);
    const id = location.pathname.match(/\/jobs\/view\/(?:[^/]*-)?(\d+)\/?$/)?.[1];
    const banner = id && document.getElementById('JobDetails_ManageJobBanner_' + id);
    if (banner && visible(banner.nextElementSibling)) roots.push(banner.nextElementSibling);
    return [...new Set(roots)];
}"""

DETAILS_JS = r"""() => {
    const roots = (""" + LINKEDIN_ROOTS_JS + r""")();
    const clean = s => (s || '').replace(/\s+/g, ' ').trim();
    let title = '', company = '';
    const parts = document.title.split('|').map(clean).filter(x => x && x !== 'LinkedIn');
    for (const root of roots) {
        company = [...root.querySelectorAll('a[href*="/company/"]')].map(e => clean(e.innerText)).find(Boolean) || company;
        title = [...root.querySelectorAll('h1, [role=heading][aria-level="1"], .job-details-jobs-unified-top-card__job-title')].map(e => clean(e.innerText)).find(Boolean) || title;
        if (!title) title = [...root.querySelectorAll('p')].map(e => clean(e.innerText))
            .find(s => s !== company && (parts.includes(s) || parts.join(' | ').includes(s))) || '';
        if (title && company) break;
    }
    // A tab-title segment alone never establishes identity. Remove a company
    // prefix only when the employer link corroborates it (QuintoAndar layout).
    if (title.includes('|') && company && title.split('|')[0].toLowerCase().includes(company.toLowerCase()))
        title = title.split('|').slice(1).join('|').trim();
    const id = location.pathname.match(/\/jobs\/view\/(?:[^/]*-)?(\d+)\/?$/)?.[1];
    const about = id && document.getElementById('JobDetails_AboutTheJob_' + id);
    const desc = about?.querySelector('[data-testid="expandable-text-box"]') || about || document.querySelector('#job-details, .jobs-description');
    return {title, company, description: desc?.innerText || '', roots: roots.length};
}"""

METADATA_JS = r"""source => {
    const postings = [];
    const visible = e => e && e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden';
    const remote = source === 'remoteok' ? (""" + REMOTEOK_ROOT_JS + r""")() : {root:null,id:null};
    const remoteRoot = remote.root, remoteId = remote.id;
    function visit(value) {
        if (Array.isArray(value)) return value.forEach(visit);
        if (!value || typeof value !== 'object') return;
        const types = [].concat(value['@type'] || []);
        if (types.some(t => /(^|[/#])JobPosting$/.test(t))) postings.push(value);
        if (value['@graph']) visit(value['@graph']);
        if (value.mainEntity) visit(value.mainEntity);
    }
    const scripts = source === 'remoteok' ? (remoteRoot?.querySelectorAll('script[type="application/ld+json"]') || [])
        : document.querySelectorAll('script[type="application/ld+json"]');
    for (const el of scripts) {
        try { visit(JSON.parse(el.textContent)); } catch (_) {}
    }
    // Only job-detail metadata areas; never search the description or recommendations.
    const selectors = {
        linkedin: '.job-details-jobs-unified-top-card, .jobs-unified-top-card, .job-details-fit-level-preferences',
        wellfound: '[data-test="job-header"], [data-testid="job-header"], [data-test="job-detail-header"]',
        indeed: '#jobDetailsSection, #jobLocationText, .jobsearch-JobMetadataFooter, [data-testid="job-age"]'
    };
    const roots = source === 'remoteok' ? (remoteRoot ? [remoteRoot] : [])
        : source === 'linkedin' ? (""" + LINKEDIN_ROOTS_JS + r""")() : [...document.querySelectorAll(selectors[source] || '[data-autoapply-job-metadata]')]
        .filter(e => e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden');
    const remoteLabels = remoteRoot ? [...remoteRoot.querySelectorAll('.location, [itemprop="jobLocationType"], [data-workplace-type]')].filter(visible) : [];
    return {postings, title: source === 'remoteok' ? (remoteRoot?.querySelector('[itemprop="title"], h2')?.innerText || '')
            : (document.querySelector('h1')?.innerText || ''),
        labels: source === 'remoteok' ? remoteLabels.map(e => e.getAttribute('data-workplace-type') || e.innerText) : roots.map(e => e.innerText),
        scoped_job_id: remoteRoot ? remoteId : null,
        language: document.documentElement.lang, roots: roots.length,
        times: roots.flatMap(e => [...e.querySelectorAll('time[datetime]')].map(t => t.dateTime))};
}"""


def page_metadata(page, source: str, expected_url: str, *, attempts: int = 3) -> dict:
    raw = page.evaluate(METADATA_JS, source)
    inspections = 1
    for _ in range(max(0, min(attempts, 5) - 1)):
        candidates = [*raw.get('labels', []), *raw.get('times', []),
                      *(p.get('datePosted') for p in raw.get('postings', []))]
        if any(posting_time(value) is not None for value in candidates):
            break
        page.wait_for_timeout(1000)
        raw = page.evaluate(METADATA_JS, source)
        inspections += 1
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
        urls = [urljoin(expected_url, u) for u in urls if isinstance(u, str) and u.strip()]
        if any(urlparse(u).scheme == 'https' and identity(u) == expected for u in urls):
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
    header_location = next((line.split('·')[0].strip() for label in labels for line in label.splitlines()
                            if '·' in line and posting_time(line) is not None), '')
    return {"posted_at": posted,
            "workplace": workplace_kind(posting.get("jobLocationType"), *labels), "location": location or header_location,
            'title': posting.get('title', ''),
            'company': (posting.get('hiringOrganization') or {}).get('name', '') if isinstance(posting.get('hiringOrganization'), dict) else '',
            'description': posting.get('description', ''),
            'diagnostic': {'language': raw.get('language', ''), 'header_roots': raw.get('roots', len(labels)),
                           'inspections': inspections,
                            'jsonld_postings': len(raw.get('postings', [])), 'scoped_job_id': raw.get('scoped_job_id'),
                            'date_evidence': [v[:250] for v in dates if isinstance(v, str) and posting_time(v) is not None],
                           'reason': None if posted else 'No parseable date in matched job header/structured data after bounded hydration wait'}}
