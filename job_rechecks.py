"""Bounded re-evaluation of pre-submission decisions; never unlock submissions."""
import copy
import csv
import datetime as dt
import hashlib
import json
from pathlib import Path
import shutil
from job_relevance import VERSION as RELEVANCE_REVISION

RECHECKABLE = frozenset({'metadata_manual', 'title_not_relevant', 'relevance_manual',
                       'required_stack_incompatible', 'specialized_role', 'excluded_technology', 'title_keyword_excluded'})
PARSER_REVISION = 'linkedin-details-v2+remoteok-detail-v2'


def revision(config: dict, settings: dict) -> str:
    content = {'parser': PARSER_REVISION, 'relevance_revision': RELEVANCE_REVISION, 'filters': config['filters'], 'search': config['search'],
               'target_technologies': settings.get('target_technologies', []),
               'excluded_technologies': settings.get('excluded_technologies', []),
               'excluded_title_words': settings.get('excluded_title_words', []),
               'job_search_titles': settings.get('job_search_titles'),
               'profile_country': settings.get('cv_profile', {}).get('country')}
    return hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()


def backup(files, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    folder = directory / ('history-' + dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    folder.mkdir()
    for path in files:
        if path.exists():
            shutil.copy2(path, folder / path.name)
    return folder


def snapshot(value: dict) -> dict:
    return copy.deepcopy({key: val for key, val in value.items() if key not in ('decisions', 'recheck')})


def prepare(applied: dict, files, directory: Path) -> bool:
    if not any(value['status'] in RECHECKABLE and 'decisions' not in value for value in applied.values()):
        return False
    backup(files, directory)
    reasons = {}
    for path in files:
        if path.name == 'manual_apply.csv' and path.exists():
            with path.open(encoding='utf-8', newline='') as stream:
                reasons = {row.get('job_id'): row.get('reason') for row in csv.DictReader(stream)}
    for job_id, value in applied.items():
        if value['status'] in RECHECKABLE and 'decisions' not in value:
            value.setdefault('reason', reasons.get(job_id) or value['status'])
            value['decisions'] = [snapshot(value)]
    return True


def claim(value: dict, revision_id: str, run_state: dict, *, now=None, max_per_run=10, max_per_job=3, cooldown_hours=24) -> bool:
    for limit, upper in [(max_per_run, 100), (max_per_job, 10), (cooldown_hours, 720)]:
        if type(limit) is not int or not 0 <= limit <= upper:
            raise ValueError('Invalid bounded recheck limits')
    if value.get('status') not in RECHECKABLE or run_state['count'] >= max_per_run:
        return False
    now = now or dt.datetime.now(dt.timezone.utc)
    previous = value.get('recheck', {})
    if not isinstance(previous, dict) or type(previous.get('count', 0)) is not int or previous.get('count', 0) < 0:
        return False
    if previous.get('count', 0) >= max_per_job or previous.get('run_id') == run_state['id']:
        return False
    if previous.get('revision') == revision_id:
        try:
            last = dt.datetime.fromisoformat(previous['at'])
            if last.tzinfo is None or (now - last).total_seconds() < cooldown_hours * 3600:
                return False
        except (KeyError, ValueError, TypeError):
            return False
    value['recheck'] = {'count': previous.get('count', 0) + 1, 'at': now.isoformat(),
                        'revision': revision_id, 'run_id': run_state['id']}
    run_state['count'] += 1
    return True
