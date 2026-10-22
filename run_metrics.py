"""Observational per-run counters, independent of durable submission budgets."""
from contextlib import contextmanager


class RunMetrics:
    STAGES = ('seen', 'history_skipped', 'evaluated', 'rejected', 'manual', 'errors',
              'forms_opened', 'forms_changed', 'previews', 'confirmed', 'searches_verified', 'searches_failed')

    def __init__(self):
        self.reset()

    def reset(self):
        self.jobs = {stage: set() for stage in self.STAGES}
        self.current = None

    def mark(self, stage, job_id=None):
        job_id = job_id or self.current
        if job_id:
            self.jobs[stage].add(job_id)

    @contextmanager
    def application(self, job_id):
        previous = self.current
        self.current = job_id
        try:
            yield
        finally:
            self.current = previous

    def outcome(self, job_id, status):
        if status == 'dry_run':
            self.mark('previews', job_id)
        elif status in ('applied', 'applied_external'):
            self.mark('confirmed', job_id)
        elif status in ('pre_submission_error', 'error'):
            self.mark('errors', job_id)
        else:
            # Blocked, unsupported and uncertain outcomes are not filled forms.
            self.mark('manual', job_id)

    def snapshot(self):
        return {stage: len(ids) for stage, ids in self.jobs.items()}

    def summary(self, submit_attempts=0):
        counts = self.snapshot()
        return ' | '.join(f'{stage}: {value}' for stage, value in counts.items()) + f' | submit_attempts: {submit_attempts}'
