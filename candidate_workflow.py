"""Persistent candidate evaluation shared by bulk and chat-assisted LinkedIn."""
import hashlib
import os
import json
from datetime import date

import application_policy as policy
from application_queue import Store
import resume_match


def store(core):
    return Store(core.HOME / 'data' / 'candidates.json')


def save(core, job, **fields):
    return store(core).put(job['id'], title=job['title'], company=job['company'],
                           job_url=job['url'], description=job.get('desc', ''),
                           metadata=job.get('metadata', {}), policy_version=policy.VERSION, **fields)


def account_restriction(core, company):
    record = store(core).load()['accounts'].get(company.strip().casefold())
    if not record or record.get('status') != 'cooldown':
        return None
    retry_after = date.fromisoformat(record['retry_after'])
    return record if date.today() < retry_after else None


def observation_block(core, job_id, *, company='', title=''):
    path = core.HOME / 'data' / 'application_observations.json'
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data, dict) or data.get('version') != 1 or not isinstance(data.get('jobs'), dict):
        raise ValueError('Invalid application observations; do not bypass duplicate protection')
    for observed_id, entry in data['jobs'].items():
        same_role = (company and title and entry.get('company', '').strip().casefold() == company.strip().casefold()
                     and resume_match.normalized(entry.get('title', '')) == resume_match.normalized(title))
        if (observed_id == job_id or same_role) and entry.get('kind') == 'possible_duplicate' and entry.get('resolved') is not True:
            return entry
    return None


def evaluate(core, job):
    """Always preserve the JD before model/network work; no-key means pending."""
    cv = core.ROOT / 'CV_Andre2026.pdf'
    digest = hashlib.sha256(cv.read_bytes()).hexdigest()
    jd_hash = hashlib.sha256(job.get('desc', '').encode()).hexdigest()
    previous = store(core).load()['jobs'].get(job['id'], {})
    observation = observation_block(core, job['id'], company=job['company'], title=job['title'])
    if observation:
        return save(core, job, status='possible_duplicate', reason=observation['message'],
            resume_sha256=digest, assessment=previous.get('assessment', {'score': None}),
            qualifies=previous.get('qualifies', False),
            blockers=[{'kind': 'duplicate', 'verdict': 'unknown', 'explanation': observation['message']}])
    restriction = account_restriction(core, job['company'])
    if restriction:
        return save(core, job, status='account_cooldown', retry_after=restriction['retry_after'],
            reason=restriction.get('reason', 'Shared application portal has an account cooldown'),
            resume_sha256=digest, assessment=previous.get('assessment', {'score': None}),
            qualifies=previous.get('qualifies', False),
            blockers=[{'kind': 'account', 'verdict': 'unmet', 'explanation': restriction.get('reason'),
                       'retry_after': restriction['retry_after']}])
    cached = previous.get('assessment', {})
    save(core, job, status='assessing', resume_sha256=digest)
    try:
        if (cached.get('version') == resume_match.VERSION and cached.get('resume_sha256') == digest
                and cached.get('jd_sha256') == jd_hash and cached.get('score') is not None):
            # Recompute from stored source evidence; never trust a hand-edited score.
            evidence = resume_match.from_file(job.get('desc', ''), cv, minimum_percent=70,
                review={'complete': True, 'criteria': cached['criteria'], 'blockers': cached['blockers']},
                reviewer=cached.get('reviewer', 'saved-evidence'))
        elif os.getenv('AUTOAPPLY_ASSESSMENT_MODE', 'auto') == 'manual':
            evidence = resume_match.from_file(job.get('desc', ''), cv, minimum_percent=70)
            if job.get('desc', '').strip() and evidence.get('score') is None:
                evidence['diagnostic'] = 'Manual evidence assessment requested for this run'
        else:
            config = core.current_llm_config()
            if not config.legacy_supported:
                raise ValueError('Selected provider cannot assess JD; use a manual evidence review')
            key = core.find_key()
            evidence = resume_match.from_file(job.get('desc', ''), cv, minimum_percent=70,
                key=key, log=core.log, max_output_tokens=config.max_output_tokens,
                timeout_seconds=config.timeout_seconds)
            if not key and job.get('desc', '').strip() and evidence.get('score') is None:
                evidence['diagnostic'] = 'No OpenRouter key configured; manual evidence assessment required'
    except Exception as exc:
        safe_messages = ('Selected provider cannot assess JD', 'No free model returned',
                         'Full JD + CV exceeds', 'CV changed during assessment',
                         'JD assessment incomplete', 'Criterion quote is not',
                         'CV quote is not', 'Supported criteria require', 'No substantive non-optional')
        diagnostic = (str(exc) if isinstance(exc, ValueError) and str(exc).startswith(safe_messages)
                      else 'JD assessment unavailable: ' + type(exc).__name__)
        if type(getattr(exc, 'code', None)) is int:
            diagnostic += ' (HTTP ' + str(exc.code) + ')'
        evidence = {'version': resume_match.VERSION, 'threshold': 70, 'score': None,
                    'reason': 'resume_match_unknown', 'resume_sha256': digest, 'jd_sha256': jd_hash,
                    'diagnostic': diagnostic, 'criteria': [], 'blockers': []}
    location, geo = policy.job_geography(job)
    blockers = list(evidence.get('blockers', []))
    if geo:
        blockers.append({'kind': 'geography', 'verdict': 'unmet' if geo == 'geography_excluded' else 'unknown',
                         'explanation': geo, 'location': location})
    score = evidence.get('score')
    status = ('needs_assessment' if score is None else 'below_threshold' if score < 70 else
              'eligible_blocked' if blockers else 'eligible')
    if geo == 'geography_excluded':
        status = 'geography_excluded'
    entry = save(core, job, status=status, assessment=evidence, blockers=blockers,
                 qualifies=score is not None and score >= 70,
                 location=location,
                 reason=evidence.get('reason') or geo, resume_sha256=digest,
                 activity='unverified_until_apply_control', application_url=previous.get('application_url', ''))
    return entry


def apply_review(core, job_id, review, *, reviewer='agent-reviewed'):
    """Manual evidence input uses the same quote validation and local arithmetic."""
    entry = store(core).load()['jobs'][job_id]
    cv = core.ROOT / 'CV_Andre2026.pdf'
    if hashlib.sha256(cv.read_bytes()).hexdigest() != entry.get('resume_sha256'):
        raise ValueError('CV changed; rediscover/reassess this candidate first')
    evidence = resume_match.from_file(entry['description'], cv, review=review, reviewer=reviewer)
    store(core).put(job_id, assessment=evidence)
    return evaluate(core, {'id': job_id, 'title': entry['title'], 'company': entry['company'],
                           'url': entry['job_url'], 'desc': entry['description'], 'metadata': entry['metadata']})


def track(core, job_id, status, **fields):
    if job_id in store(core).load()['jobs']:
        if status in ('applied', 'applied_external'):
            fields.update(activity='application_confirmed', outcome_confirmation='explicit_site_confirmation')
        store(core).put(job_id, status=status, **fields)


def submission_allowed(core, job_id):
    """Defense in depth: no active-policy send on a stale/unreviewed candidate."""
    entry = store(core).load()['jobs'].get(job_id, {})
    if observation_block(core, job_id, company=entry.get('company', ''), title=entry.get('title', '')):
        return False
    if account_restriction(core, entry.get('company', '')):
        return False
    assessment = entry.get('assessment', {})
    cv = core.ROOT / 'CV_Andre2026.pdf'
    score = assessment.get('score')
    return (entry.get('qualifies') is True and type(score) in (int, float) and score >= 70
            and assessment.get('version') == resume_match.VERSION and not entry.get('blockers')
            and cv.is_file() and hashlib.sha256(cv.read_bytes()).hexdigest() == entry.get('resume_sha256')
            and (entry.get('metadata') or {}).get('active') is not False
            and (entry.get('metadata') or {}).get('workplace') == 'remote')
