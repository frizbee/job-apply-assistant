"""remoteok platform adapter; policy/sending guards are injected shared services."""
import datetime as dt
import html
import json
import re
import time
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus, urlparse
from playwright.sync_api import Page, Locator
from .base import PlatformAdapter

REMOTEOK_ROOT_JS = r"""() => {
    const visible = e => e && e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden';
    const id = location.pathname.match(/^\/remote-jobs\/[^/?#]+-(\d+)\/?$/)?.[1]
        || location.pathname.match(/^\/(?:remote-jobs|jobs)\/(\d+)\/?$/)?.[1];
    if (location.protocol !== 'https:' || !['remoteok.com', 'www.remoteok.com'].includes(location.hostname)) return {root:null,id};
    const root = id && document.getElementById('job-' + id);
    if (!root || !root.matches('tr.job') || root.getAttribute('data-id') !== id || !visible(root)) return {root:null,id};
    try {
        const target = new URL(root.getAttribute('data-href'), location.href);
        if (target.origin !== location.origin || target.pathname.replace(/\/$/, '') !== location.pathname.replace(/\/$/, '')) return {root:null,id};
    } catch (_) { return {root:null,id}; }
    return {root,id};
}"""

REMOTEOK_DETAIL_JS = r"""() => {
    const {root,id} = (""" + REMOTEOK_ROOT_JS + r""")();
    if (!root) return {issue:'detail_identity_unknown'};
    const detail = root.nextElementSibling;
    const visible = e => e && e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden';
    if (!detail?.matches('tr.expand') || detail.getAttribute('data-id') !== id || !visible(detail))
        return {id,issue:'description_unknown'};
    const bodies = [...detail.querySelectorAll('.description > .html, .description > .markdown')].filter(visible);
    const links = [...detail.querySelectorAll('a.action-apply')].filter(a => visible(a) && a.getAttribute('data-job-id') === id);
    const parts = [];
    if (bodies.length === 1) {
        const walker = document.createTreeWalker(bodies[0], NodeFilter.SHOW_TEXT);
        while (walker.nextNode()) {
            const node = walker.currentNode, parent = node.parentElement;
            if (visible(parent) && !parent.closest('a.action-apply, button, input, script, style') && node.textContent.trim())
                parts.push(node.textContent.trim());
        }
    }
    return {id,description:parts.join('\n'),
        apply_links:links.map(a=>({href:a.href,disabled:a.getAttribute('aria-disabled') === 'true'}))};
}"""


def complete_description(text):
    # Board-added attention-check boilerplate must not disguise a cut-off body.
    body = re.split(r'\bplease mention the word\b', text, maxsplit=1, flags=re.I)[0].strip()
    cut = re.search(r'(?:\.\.\.|…|â€¦)(?:\s+Apply now and work remotely at\b[\s\S]*)?\s*$', body, re.I)
    return bool(body) and cut is None


def combine_descriptions(*texts):
    """Keep board instructions from the feed as well as the scoped full body."""
    result = []
    for text in texts:
        text = text.strip()
        normalized = ' '.join(text.split())
        if not normalized or any(normalized in ' '.join(other.split()) for other in result):
            continue
        result = [other for other in result if ' '.join(other.split()) not in normalized]
        result.append(text)
    return '\n\n'.join(result)


class RemoteOKAdapter(PlatformAdapter):
    def __init__(self, services):
        super().__init__(services.core, services)

    def job_identity(self, job):
        match = re.fullmatch(r'remoteok:(\d+)', job.get('id', ''))
        parsed = urlparse(job.get('url', ''))
        path_id = re.fullmatch(r'/(?:remote-jobs/[^/?#]+-|(?:remote-jobs|jobs)/)(\d+)/?', parsed.path)
        if (not match or not path_id or match[1] != path_id[1] or parsed.scheme != 'https'
                or parsed.hostname not in ('remoteok.com', 'www.remoteok.com') or parsed.username or parsed.password):
            return None
        return match[1]

    def prepare_job(self, page, job):
        """Hydrate after history checks, before any description-dependent filters."""
        try:
            if self.core.STOP.is_set():
                raise self.core.Stopped
            if time.time() > self.core.DEADLINE:
                raise self.core.LimitReached
            self._prepare_job(page, job)
        except (self.core.Stopped, self.core.LimitReached, self.core.BrowserClosed):
            raise
        except Exception as error:
            if self.core.browser_closed(error):
                raise self.core.BrowserClosed() from None
            job['_remoteok_detail_loaded'] = False
            job['detail_issue'] = 'description_unknown'
            job['metadata'] = {**(job.get('metadata') or {}), 'diagnostic': {
                'reason': 'Remote OK detail inspection failed; feed text alone was not accepted',
                'error_type': type(error).__name__}}
            self.core.METRICS.mark('errors', job['id'])
            self.core.log(f"  ! Remote OK details could not be inspected ({type(error).__name__}); deferring this job")

    def _prepare_job(self, page, job):
        job.setdefault('_remoteok_feed_desc', job.get('desc', ''))
        job.pop('detail_issue', None)
        job.pop('detail_source_blocked', None)
        job['_remoteok_detail_loaded'] = False
        jid = self.job_identity(job)
        if not jid:
            job['detail_issue'] = 'detail_identity_unknown'
            return
        current, expected = urlparse(page.url), urlparse(job['url'])
        if (current.scheme, current.hostname, current.path.rstrip('/')) != (expected.scheme, expected.hostname, expected.path.rstrip('/')):
            if not self.services.goto(page, job['url'], 'Remote OK'):
                job['detail_issue'] = 'description_unknown'
                job['detail_source_blocked'] = True
                return
        metadata = self.core.read_job_metadata(page, 'remoteok', job['url'])
        detail = page.evaluate(REMOTEOK_DETAIL_JS)
        same = lambda a, b: ' '.join((a or '').split()).casefold() == ' '.join((b or '').split()).casefold()
        if (detail.get('id') != jid or (metadata.get('diagnostic') or {}).get('scoped_job_id') != jid
                or not same(metadata.get('title'), job['title']) or not metadata.get('title')
                or not same(metadata.get('company'), job['company']) or not metadata.get('company')):
            job['detail_issue'] = 'detail_identity_unknown'
            return
        native = detail.get('description') or ''
        structured = self.services.strip_html(metadata.get('description') or '')
        full = [text for text in (native, structured) if complete_description(text)]
        job['metadata'] = self.services.job_metadata.merge_metadata(job.get('metadata') or {}, metadata)
        job['desc'] = combine_descriptions(*full, job['_remoteok_feed_desc'])
        if detail.get('issue') or not full:
            job['detail_issue'] = 'description_unknown'
            job['metadata']['diagnostic'] = {**(job['metadata'].get('diagnostic') or {}),
                                            'reason': 'Full current-job description is missing, ambiguous or ends with an ellipsis; feed text alone is not accepted'}
            return
        job['_remoteok_native_description'] = native
        job['_remoteok_detail_loaded'] = True

    def application_link(self, page, job):
        """Accept only current-job native /l/ID links, never other page buttons."""
        jid = self.job_identity(job)
        detail = page.evaluate(REMOTEOK_DETAIL_JS)
        if (not jid or detail.get('id') != jid or detail.get('issue') or not job.get('_remoteok_detail_loaded')
                or detail.get('description') != job.get('_remoteok_native_description')):
            return None
        links = detail.get('apply_links') or []
        if not links:
            return None
        targets = set()
        for link in links:
            parsed = urlparse(link.get('href', ''))
            if (link.get('disabled') or parsed.scheme != 'https' or parsed.hostname not in ('remoteok.com', 'www.remoteok.com')
                    or parsed.username or parsed.password or parsed.path.rstrip('/') != f'/l/{jid}' or parsed.query or parsed.fragment):
                return None
            targets.add(link['href'])
        if len(targets) != 1:
            return None
        root = page.locator(f'tr.job#job-{jid}').locator('xpath=following-sibling::*[1]')
        controls = root.locator(f'a.action-apply[data-job-id="{jid}"]')
        visible = [control for control in controls.all() if control.is_visible()]
        if not visible or any(control.evaluate('a=>a.href') not in targets or control.get_attribute('aria-disabled') == 'true'
                              for control in visible):
            return None
        return visible[0]

    def remoteok(self, page):
        feeds = [''] + [f'?tag={self.services.slug(kw)}' for kw in self.core.CFG['search']['keywords']] + ['?tag=ai', '?tag=machine-learning']
        jobs = {}
        for q in dict.fromkeys(feeds):
            for j in json.loads(self.services.http(f'https://remoteok.com/api{q}'))[1:]:
                jobs[j['id']] = j
            time.sleep(1)
        for j in jobs.values():
            if not self.services.keyword_match(j.get('position', '')):
                continue
            job = {'id': f'remoteok:{j['id']}', 'title': j['position'], 'company': j.get('company', ''), 'url': j['url'], 'desc': self.services.strip_html(j.get('description', '')), 'location': j.get('location', ''), 'metadata': {'posted_at': j.get('epoch') or j.get('date'), 'workplace': self.services.job_metadata.workplace_kind(j.get('workplaceType'), j.get('remote'), j.get('location')), 'location': j.get('location', '')}}

            def apply(applied=None, job=job):
                control = self.application_link(page, job)
                if control is None:
                    self.core.record_manual(job['id'], job['title'], job['company'], job['url'],
                                            'Current-job Remote OK Apply link/description could not be confirmed; no global Apply button was clicked')
                    return 'external_manual'
                return self.services.external(page, job, self.core.follow_apply(page, control), 'remoteok.com', applied)
            yield (job, apply)
