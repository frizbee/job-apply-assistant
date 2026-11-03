"""The GUI's execution mode must survive in every durable decision."""
import csv
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

import apply
from test_answer_safety import SafetyTest


class HistoryModeTests(SafetyTest):
    def setUp(self):
        super().setUp()
        self.folder = self.enterContext(tempfile.TemporaryDirectory())
        self.home = Path(self.folder)
        for attr, name in [('APPLIED_FILE', 'applied.json'), ('LOG_FILE', 'applications.csv'),
                           ('MANUAL_FILE', 'manual_apply.csv')]:
            self.enterContext(patch.object(apply, attr, self.home / name))

    def test_each_decision_and_csv_row_preserves_its_mode(self):
        history = {}
        for test_mode in [True, False]:
            with patch.object(apply, 'DRY_RUN', test_mode):
                apply.record(history, '1', 'Role', 'Company', 'manual_review')
                apply.record_manual('1', 'Role', 'Company', 'https://example.com', 'Review')
        stored = json.loads(apply.APPLIED_FILE.read_text())['1']
        self.assertIs(stored['test_mode'], False)
        self.assertEqual([d['test_mode'] for d in stored['decisions']], [True, False])
        for path in [apply.LOG_FILE, apply.MANUAL_FILE]:
            with path.open() as stream:
                self.assertEqual([r['test_mode'] for r in csv.DictReader(stream)], ['true', 'false'])

    def test_legacy_rows_are_backed_up_and_not_falsely_marked_live(self):
        original = 'date,job_id,title,company,status,url\n2026-10-22,1,Role,Company,manual_review,https://example.com\n'
        apply.LOG_FILE.write_text(original)
        with patch.object(apply, 'DRY_RUN', True):
            apply.record({}, '2', 'Role', 'Company', 'manual_review')
        with apply.LOG_FILE.open() as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual([r['test_mode'] for r in rows], ['', 'true'])
        backups = list((self.home / 'backups').glob('*/applications.csv'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), original)
