"""User data stays UTF-8 even when the process default text encoding is ASCII."""
import ast
import csv
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import MagicMock, patch

from test_answer_safety import SafetyTest
import apply


class Utf8PersistenceTests(SafetyTest):
    def setUp(self):
        super().setUp()
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory(prefix='utf8-persistence-')))
        self.title = 'Разработчик 日本語 🚀'
        self.company = 'Компания 東京'
        self.enterContext(patch.object(apply, 'APPLIED_FILE', self.home / 'applied.json'))
        self.enterContext(patch.object(apply, 'LOG_FILE', self.home / 'applications.csv'))
        self.enterContext(patch.object(apply, 'MANUAL_FILE', self.home / 'manual_apply.csv'))

    def ascii_process(self, script):
        # Keep argv ASCII too: only the application data, not Python source decoding,
        # should be under test. No browser or network calls are made by these scripts.
        result = subprocess.run([sys.executable, '-c', script.encode('ascii', 'backslashreplace').decode('ascii')],
            cwd=Path(apply.__file__).parent, env={**os.environ, 'AUTOAPPLY_HOME': str(self.home),
                'LC_ALL': 'C', 'LANG': 'C', 'PYTHONUTF8': '0', 'PYTHONCOERCECLOCALE': '0',
                'PYTHONIOENCODING': 'ascii', 'OPENROUTER_API_KEY': ''}, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', errors='replace'))

    def test_ascii_process_writes_and_reloads_unicode_history_settings_and_env(self):
        self.ascii_process(r'''
import json, locale, os, sys
from pathlib import Path
import apply, cv_profile
assert sys.flags.utf8_mode == 0
if os.name != 'nt':
    assert locale.getencoding().lower() in ('ascii', 'us-ascii', 'ansi_x3.4-1968')
title, company = 'Разработчик 日本語 🚀', 'Компания 東京'
state = {}
apply.record(state, 'job:東京', title, company, 'submission_pending')
assert apply.load_applied() == state
assert title.encode('utf-8') in apply.APPLIED_FILE.read_bytes()
apply.record_manual('job:東京', title, company, 'https://example.test/job', 'Проверить 手動')
prof = cv_profile.empty_profile()
prof.update(first_name='Иван', last_name='田中', email='test@example.com', city='Москва',
    country='日本', total_years=3, job_titles=[title])
resume = str(apply.HOME / 'Резюме 履歴書.pdf')
apply.SETTINGS.update(resume=resume, salary='60000', currency='USD', english='3', notice='14', cv_profile=prof)
apply.finalize_settings()
expected = dict(apply.SETTINGS)
apply.SETTINGS.clear()
assert apply.load_settings() == expected
assert apply.SETTINGS['resume'] == resume
env = apply.HOME / '.env'
env.write_bytes('# комментарий 日本語\nOTHER=значение\n'.encode('utf-8'))
apply.save_key('test-key')
assert apply.find_key() == 'test-key'
assert 'OTHER=значение' in env.read_bytes().decode('utf-8')
''')

    def test_ascii_process_reads_existing_utf8_data(self):
        data = self.home / 'data'
        data.mkdir()
        state = {'job': {'status': 'submission_pending', 'title': self.title, 'company': self.company}}
        (data / 'applied.json').write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8')
        (data / 'user_settings.json').write_text(json.dumps({'resume': 'Резюме 履歴書.pdf'}, ensure_ascii=False), encoding='utf-8')
        (self.home / '.env').write_text('# 日本語\nOPENROUTER_API_KEY=test-key\n', encoding='utf-8')
        self.ascii_process('''
import apply
assert apply.load_applied()['job']['title'] == 'Разработчик 日本語 🚀'
assert apply.load_settings()['resume'] == 'Резюме 履歴書.pdf'
assert apply.find_key() == 'test-key'
''')

    def test_unicode_directory_and_csv_round_trip(self):
        folder = self.home / 'Данные 東京'
        folder.mkdir()
        with patch.object(apply, 'APPLIED_FILE', folder / 'история.json'):
            apply.record({}, 'job', self.title, self.company, 'submission_pending')
            self.assertEqual(apply.load_applied()['job']['title'], self.title)
        apply.record_manual('job', self.title, self.company, 'https://example.test/', 'Проверить')
        for path, field in [(apply.LOG_FILE, 'title'), (apply.MANUAL_FILE, 'title')]:
            with path.open(encoding='utf-8', newline='') as stream:
                self.assertEqual(next(csv.DictReader(stream))[field], self.title)

    def test_failed_atomic_replace_keeps_previous_history(self):
        original = {'old': {'status': 'applied', 'title': self.title}}
        apply.write_applied(original)
        before = apply.APPLIED_FILE.read_bytes()
        with patch.object(Path, 'replace', side_effect=OSError('disk failure')):
            with self.assertRaises(OSError):
                apply.write_applied({'new': {'status': 'submission_pending', 'title': self.title}})
        self.assertEqual(apply.APPLIED_FILE.read_bytes(), before)
        self.assertEqual(apply.load_applied(), original)

    def test_invalid_utf8_history_is_not_treated_as_empty(self):
        apply.APPLIED_FILE.write_bytes(b'\xff')
        with self.assertRaisesRegex(RuntimeError, 'history is corrupt'):
            apply.load_applied()
        self.assertEqual(apply.APPLIED_FILE.read_bytes(), b'\xff')

    def test_invalid_utf8_settings_do_not_overwrite_memory_or_disk(self):
        path = self.home / 'settings.json'
        path.write_bytes(b'\xff')
        before = dict(apply.SETTINGS)
        with patch.object(apply, 'SETTINGS_FILE', path):
            with self.assertRaises(UnicodeDecodeError):
                apply.load_settings()
        self.assertEqual(apply.SETTINGS, before)
        self.assertEqual(path.read_bytes(), b'\xff')

    def test_unicode_legacy_history_migration_preserves_details(self):
        original = {'job': {'status': 'skipped_unconfirmed', 'title': self.title, 'company': self.company}}
        apply.APPLIED_FILE.write_text(json.dumps(original, ensure_ascii=False), encoding='utf-8')
        migrated = apply.load_applied()
        self.assertEqual(migrated['job']['title'], self.title)
        self.assertEqual(migrated['job']['status'], 'submission_unconfirmed')
        self.assertEqual(apply.load_applied(), migrated)
        with apply.MANUAL_FILE.open(encoding='utf-8', newline='') as stream:
            self.assertEqual(next(csv.DictReader(stream))['title'], self.title)

    def test_pending_write_failure_prevents_submit_click(self):
        button = MagicMock()
        with patch.object(apply, 'DRY_RUN', False), patch.object(Path, 'write_text', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                apply.submit_application(MagicMock(), button, 'job', self.title, self.company,
                    'https://example.test/', lambda: 'applied', {})
        button.click.assert_not_called()

    def test_application_text_io_always_declares_utf8(self):
        for name in ['apply.py', 'gui.py', 'cv_profile.py', 'sources.py', 'job_metadata.py']:
            tree = ast.parse((Path(apply.__file__).parent / name).read_text(encoding='utf-8'))
            for call in ast.walk(tree):
                if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) and call.func.attr in {'read_text', 'write_text', 'open'}:
                    # webbrowser.open is not file I/O.
                    if isinstance(call.func.value, ast.Name) and call.func.value.id == 'webbrowser':
                        continue
                    encoding = next((k.value for k in call.keywords if k.arg == 'encoding'), None)
                    self.assertIsInstance(encoding, ast.Constant, (name, call.lineno))
                    self.assertEqual(encoding.value, 'utf-8', (name, call.lineno))
