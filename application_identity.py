"""Idempotency by final ATS job ID, not board ID, title or tracking parameters.

One GUI runtime serializes active work. This is not a distributed/multi-process
transaction coordinator; pending/unconfirmed history remains fail-closed.
"""
import csv
from platforms.registry import application_key


class ApplicationLedger:
    def __init__(self, namespace):
        self.namespace = namespace
        self.attempts = {}
        self.bindings = {}

    def history_owner(self, key, job_id, history, core):
        # Read old evidence without migrating or clearing the user's history.
        notes = {}
        if core.MANUAL_FILE.exists():
            with core.MANUAL_FILE.open(encoding='utf-8', newline='') as stream:
                notes = {row['job_id']: row.get('apply_url') for row in csv.DictReader(stream) if row.get('job_id')}
        session = core.deferred_review.CURRENT.get()
        explicit = bool(session and session.job_id == job_id and history.get(job_id, {}).get('status') in core.deferred_review.REVIEWABLE)
        candidates = []
        for other, entry in history.items():
            if entry.get('status') == 'external_duplicate' and entry.get('application_alias_of') == job_id:
                continue
            previous = entry.get('application_key') or application_key(entry.get('application_url') or entry.get('url', ''), core) or application_key(notes.get(other) or '', core)
            if previous != key:
                continue
            status = entry.get('status', '')
            protected = status.startswith('applied') or status in core.UNCERTAIN_STATUSES or status == 'submission_rejected'
            if explicit and not protected:
                continue
            if core.can_retry(status):
                continue
            # Prefer truly protected history over pre-submission manual rows.
            candidates.append((not protected, other))
        return sorted(candidates, key=lambda item: item[0])[0][1] if candidates else None

    def begin(self, key, job_id, url, history, core):
        if not key:
            return None
        owner = self.history_owner(key, job_id, history, core)
        owner_status = history.get(owner, {}).get('status', '') if owner is not None else ''
        if owner is not None and (owner_status.startswith('applied') or owner_status in core.UNCERTAIN_STATUSES or owner_status == 'submission_rejected'):
            self.bindings[job_id] = {'application_key': key, 'application_url': url}
            if owner != job_id:
                self.bindings[job_id]['application_alias_of'] = owner
            return 'external_duplicate'
        previous = self.attempts.get(key)
        if previous:
            if previous['job_id'] == job_id and previous.get('result'):
                return previous['result']
            self.bindings[job_id] = {'application_key': key, 'application_url': url}
            if previous['job_id'] != job_id:
                self.bindings[job_id]['application_alias_of'] = previous['job_id']
            return 'external_duplicate'
        if owner is not None:
            self.bindings[job_id] = {'application_key': key, 'application_url': url}
            if owner != job_id:
                self.bindings[job_id]['application_alias_of'] = owner
            return 'external_duplicate'
        self.attempts[key] = {'job_id': job_id, 'result': None}
        self.bindings[job_id] = {'application_key': key, 'application_url': url}
        return None

    def finish(self, key, job_id, result):
        if key and self.attempts.get(key, {}).get('job_id') == job_id:
            self.attempts[key]['result'] = result

    def abandon(self, key, job_id):
        if self.attempts.get(key, {}).get('job_id') == job_id:
            self.attempts.pop(key, None)

    def submit_blocker(self, key, job_id, history, core):
        if not key:
            return None
        attempt = self.attempts.get(key)
        if attempt and attempt['job_id'] != job_id:
            return 'external_duplicate'
        owner = self.history_owner(key, job_id, history, core)
        return 'external_duplicate' if owner is not None and owner != job_id else None


def ledger(core):
    current = getattr(core, '_APPLICATION_LEDGER', None)
    if current is None or current.namespace is not core.EXT_STATE:
        current = core._APPLICATION_LEDGER = ApplicationLedger(core.EXT_STATE)
    return current


def reset(core):
    core._APPLICATION_LEDGER = ApplicationLedger(core.EXT_STATE)
