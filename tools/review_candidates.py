#!/usr/bin/env python3
"""List durable candidates or attach a complete, source-quoted manual assessment.

Does not open forms or authorize sending. Run from the project root:
  .venv/bin/python tools/review_candidates.py list
  .venv/bin/python tools/review_candidates.py assess JOB_ID review.json
  .venv/bin/python tools/review_candidates.py new-scan
"""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import apply
import application_policy
import candidate_workflow


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['list', 'assess', 'new-scan'])
    parser.add_argument('job_id', nargs='?')
    parser.add_argument('review_file', nargs='?')
    args = parser.parse_args()
    apply.load_settings()
    application_policy.activate(apply)
    store = candidate_workflow.store(apply)
    if args.action == 'new-scan':
        state = store.load()
        state['searches'] = {}  # Submission/candidate history remains intact.
        store.write(state)
        print('Discovery cursors reset. Candidate and submission history preserved.')
    elif args.action == 'assess':
        if not args.job_id or not args.review_file:
            parser.error('assess requires JOB_ID and review.json')
        result = candidate_workflow.apply_review(apply, args.job_id,
            json.loads(Path(args.review_file).read_text(encoding='utf-8')))
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        rows = store.load()['jobs']
        print(json.dumps([{'id': key, 'title': row.get('title'), 'company': row.get('company'),
                           'status': row.get('status'), 'score': row.get('assessment', {}).get('score'),
                           'blockers': row.get('blockers'), 'url': row.get('job_url')}
                          for key, row in rows.items()], ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
