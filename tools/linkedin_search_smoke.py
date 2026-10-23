#!/usr/bin/env python3
"""Live read-only LinkedIn search check using saved GUI titles/regions/filters.

No application actions, field filling, uploads or production history writes.
Private evidence is saved in ignored data/browser-diagnostics/.
"""
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ['DRY_RUN'] = 'true'
import apply
import job_metadata
import job_relevance
import linkedin_search
from playwright.sync_api import sync_playwright


def main():
    files = [apply.HOME / 'data' / name for name in ['applied.json', 'applications.csv', 'manual_apply.csv', 'user_settings.json', 'run.log']]
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in files if p.exists()}
    apply.load_settings()
    apply.apply_cv_profile(apply.SETTINGS['cv_profile'])
    apply.DRY_RUN = True
    apply.DEADLINE = min(apply.DEADLINE, time.time() + 600)
    apply.METRICS.reset()
    apply.log = lambda message: print(message, flush=True)
    folder = apply.HOME / 'data' / 'browser-diagnostics' / ('linkedin-search-review-' + dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
    folder.mkdir(parents=True)
    rows, failures, sample_ids, samples, blocked = [], [], [], [], []
    all_ids, cards = set(), {}
    original = linkedin_search.LinkedInSearch

    class ObservedSearch(original):
        def search_page(self, keyword, location, index=0):
            try:
                result = super().search_page(keyword, location, index)
            except linkedin_search.SearchUnavailable as e:
                failures.append({'keyword': keyword, 'region': location, 'page': index + 1, 'error': str(e)})
                raise
            dom = self.page.locator('[componentkey="SearchResultsMainContent"] [componentkey^="job-card-component-ref-"]').evaluate_all('es=>es.filter(e=>e.getClientRects().length).map(e=>({id:e.getAttribute("componentkey").replace("job-card-component-ref-",""),text:e.innerText.slice(0,600)}))')
            unique = {entry['id']: entry for entry in dom if re.fullmatch(r'[0-9]+', entry['id'])}
            for key, entry in unique.items():
                cards.setdefault(key, entry)
            for key in result['ids']:
                if key not in sample_ids and key in unique:
                    sample_ids.append(key)
                    break
            params = parse_qs(urlparse(self.page.url).query)
            controls = {}
            for name in ['Remote', 'Easy Apply']:
                control = self.page.locator(f'[componentkey="JobsSearchFilters"] input[aria-label="Filter by {name}"]')
                controls[name] = control.first.is_checked() if control.count() else None
            row = {'keyword': keyword, 'region': location, 'page': index + 1, 'ids': result['ids'],
                   'count': len(result['ids']), 'native_region': self.location_label(), 'visible_dom_count': len(unique),
                   'dom_ids_in_collected': set(unique) <= set(result['ids']), 'has_next': result['has_next'],
                   'filters': {key: params.get(key) for key in ['keywords', 'geoId', 'f_TPR', 'f_WT', 'f_AL', 'f_E']},
                   'native_filter_states': controls}
            rows.append(row)
            all_ids.update(result['ids'])
            (folder / (str(len(rows)) + '-search.html')).write_text(self.page.content(), encoding='utf-8')
            print('SEARCH_CHECK', json.dumps({k: v for k, v in row.items() if k not in ('ids', 'filters')}, ensure_ascii=False), flush=True)
            return result

    linkedin_search.LinkedInSearch = ObservedSearch
    def forbidden(*args, **kwargs):
        raise RuntimeError('Applications disabled in search-only diagnostic')
    apply.apply_to_job = apply.submit_application = forbidden
    fatal = None
    with sync_playwright() as pw:
        ctx = apply.launch(pw)
        ctx.add_init_script("document.addEventListener('submit',e=>{e.preventDefault();e.stopImmediatePropagation()},true)")
        def guard(route):
            req = route.request
            if req.method in ('POST', 'PUT', 'PATCH', 'DELETE') and re.search(r'/(?:apply|applications?)(?:[/\?]|$)|submitApplication|createApplication', req.url, re.I):
                blocked.append(req.url.split('?')[0])
                route.abort()
            else:
                route.continue_()
        ctx.route('**/*', guard)
        page = ctx.new_page()
        page.set_default_timeout(6000)
        page.set_default_navigation_timeout(30000)
        try:
            for ids in apply.job_lists(page):
                pass
            for job_id in sample_ids[:10]:
                try:
                    url = f'https://www.linkedin.com/jobs/view/{job_id}/'
                    page.goto(url, wait_until='domcontentloaded')
                    page.wait_for_timeout(1200)
                    title, company, desc = apply.job_info(page)
                    metadata = apply.read_job_metadata(page, 'linkedin', url)
                    reason = apply.job_ok(title, company, desc)
                    meta_reason = job_metadata.eligibility_reason(metadata, apply.CFG['search']['posted_within_hours'], apply.CFG['search'].get('remote_only', False))
                    evidence = job_relevance.assess(title, desc, **apply.technology_filters())
                    row = {'job_id': job_id, 'title': title, 'company': company, 'filter_reason': reason,
                           'metadata_reason': meta_reason, 'passes_automatic_filters': reason is None and meta_reason is None,
                           'target_stack_evidence': evidence.get('reason') or 'target_found_or_no_target_required',
                           'workplace': metadata.get('workplace'), 'posted_at': str(metadata.get('posted_at')),
                           'description_length': len(desc)}
                    samples.append(row)
                    print('DETAIL_CHECK', json.dumps(row, ensure_ascii=False), flush=True)
                except Exception as e:
                    samples.append({'job_id': job_id, 'error': type(e).__name__ + ': ' + str(e)[:250]})
        except Exception as e:
            fatal = type(e).__name__ + ': ' + str(e)[:400]
        finally:
            ctx.close()
    report = {'at': dt.datetime.now(dt.timezone.utc).isoformat(), 'forced_test_mode': True, 'search_only': True,
              'requested_titles': apply.CFG['search']['keywords'], 'requested_regions': apply.selected_locations(),
              'pages_per_search': apply.CFG['search']['pages_per_search'], 'external_enabled': apply.EXTERNAL,
              'expected_experience_levels': apply.CFG['search'].get('experience_levels'), 'rows': rows, 'failures': failures,
              'unique_jobs': len(all_ids), 'card_observations': sum(row['count'] for row in rows), 'sample_details': samples,
              'metrics': apply.METRICS.snapshot(), 'blocked_submission_requests': blocked, 'fatal': fatal,
              'production_files_unchanged': all(hashlib.sha256(p.read_bytes()).hexdigest() == value for p, value in before.items())}
    (folder / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    (folder / 'cards.json').write_text(json.dumps(cards, ensure_ascii=False, indent=2), encoding='utf-8')
    print('SUMMARY', json.dumps({k: v for k, v in report.items() if k not in ('rows', 'sample_details', 'requested_titles')}, ensure_ascii=False, indent=2))
    print('DIAGNOSTICS', folder)


if __name__ == '__main__':
    main()
