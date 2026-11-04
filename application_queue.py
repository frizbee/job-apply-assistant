"""Private, atomic candidate queue and restartable discovery progress.

Single-process writer, like the existing submission history. Never overwrite a
corrupt file, and never let candidate state authorize retry of a protected send.
"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile

PROTECTED = {'submission_pending', 'submission_unconfirmed', 'submission_rejected', 'reported_sent', 'possible_duplicate'}


def now():
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path):
        self.path = Path(path)

    def load(self):
        if not self.path.exists():
            return {'version': 1, 'jobs': {}, 'searches': {}, 'accounts': {}}
        value = json.loads(self.path.read_text(encoding='utf-8'))
        if (not isinstance(value, dict) or value.get('version') != 1
                or not isinstance(value.get('jobs'), dict) or not isinstance(value.get('searches'), dict)
                or any(not isinstance(v, dict) for v in value['jobs'].values())
                or any(not isinstance(v, dict) for v in value['searches'].values())):
            raise ValueError('Invalid candidate queue; restore a backup rather than bypassing history')
        value.setdefault('accounts', {})
        if not isinstance(value['accounts'], dict) or any(not isinstance(v, dict) for v in value['accounts'].values()):
            raise ValueError('Invalid account restrictions in candidate queue')
        return value

    def write(self, value):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=self.path.name + '.', dir=self.path.parent)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(value, stream, ensure_ascii=False, indent=2, default=str)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def put(self, job_id, **fields):
        state = self.load()
        previous = state['jobs'].get(job_id, {})
        protected = previous.get('status', '') in PROTECTED or previous.get('status', '').startswith('applied')
        if previous.get('status', '').startswith('applied') and fields.get('status') != previous['status']:
            fields.pop('status', None)
        elif protected and fields.get('status') not in PROTECTED and not fields.get('status', '').startswith('applied'):
            fields.pop('status', None)
        entry = {**previous, **fields, 'job_id': job_id, 'updated_at': now()}
        entry.setdefault('discovered_at', now())
        events = list(previous.get('events', []))
        events.append({'at': now(), 'status': entry.get('status'), 'reason': fields.get('reason')})
        entry['events'] = events
        state['jobs'][job_id] = entry
        self.write(state)
        return entry

    def progress(self, key, **fields):
        state = self.load()
        state['searches'][key] = {**state['searches'].get(key, {}), **fields, 'updated_at': now()}
        self.write(state)
        return state['searches'][key]

    def account(self, key, **fields):
        state = self.load()
        state['accounts'][key] = {**state['accounts'].get(key, {}), **fields, 'updated_at': now()}
        self.write(state)
        return state['accounts'][key]
