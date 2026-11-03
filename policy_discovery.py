"""Restartable, unbounded-by-page-count search for the approved LinkedIn policy."""
import hashlib
import json

import application_policy as policy
import candidate_workflow
import linkedin_search


def key_for(title, region):
    return hashlib.sha256(json.dumps([policy.VERSION, title, region]).encode()).hexdigest()


def job_lists(core, page):
    store = candidate_workflow.store(core)
    search = linkedin_search.PolicySearch(page, core)
    try:
        retry_states = {'needs_assessment', 'eligible', 'eligible_blocked', 'eligible_deferred_limit',
                        'discovery_error', 'waiting_for_user'}
        pending = [job_id for job_id, entry in store.load()['jobs'].items()
                   if str(job_id).isdigit() and entry.get('status') in retry_states]
        if pending:
            yield pending  # Resume durable work even when its search page is complete.
        for title in policy.TITLES:
            for region in policy.LOCATIONS:
                key = key_for(title, region)
                progress = store.load()['searches'].get(key, {})
                if progress.get('status') == 'complete':
                    continue
                # Replay/verify earlier pages to restore the site's own native
                # pagination state; only yield the unfinished page onward.
                resume_page = progress.get('next_page', 0)
                index = 0
                while True:
                    if core.STOP.is_set():
                        raise core.Stopped
                    if core.time.time() > core.DEADLINE:
                        store.progress(key, title=title, region=region, next_page=index,
                                       status='deferred_runtime')
                        raise core.LimitReached
                    try:
                        result = search.search_page(title, region, index)
                    except linkedin_search.SearchUnavailable as exc:
                        store.progress(key, title=title, region=region, next_page=max(index, resume_page),
                                       status='blocked', error=str(exc))
                        core.log(f'  ? Search deferred: {title} @ {region}: {exc}')
                        break  # Other countries/queries still get their own attempts.
                    if index >= resume_page:
                        store.progress(key, title=title, region=region, next_page=index, status='processing',
                                       job_ids=result['ids'], url=search.page.url)
                        yield result['ids']
                        store.progress(key, next_page=index + 1, status='page_processed')
                    if result.get('has_next') is False or result.get('empty_confirmed'):
                        store.progress(key, next_page=index + 1, status='complete')
                        break
                    index += 1
    finally:
        search.close()
