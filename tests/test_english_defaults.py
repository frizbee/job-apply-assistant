"""Built-in messages are English; multilingual site recognition remains supported."""
import ast
import hashlib
import io
from pathlib import Path
import re
import tempfile
import tokenize
from contextlib import redirect_stdout
from unittest.mock import MagicMock, patch

from test_answer_safety import SafetyTest, node
import apply
import cv_profile
import sources


class EnglishMessageTests(SafetyTest):
    def setUp(self):
        super().setUp()
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory(prefix='english-messages-')))
        self.resume = self.home / 'resume.pdf'
        self.resume.write_bytes(b'fixture')
        self.prof = cv_profile.empty_profile()
        self.prof.update(first_name='Test', last_name='User', current_title='Developer', total_years=3,
                         job_titles=['Software Engineer'])
        apply.SETTINGS.update(resume=str(self.resume), cv_profile=self.prof,
                              cv_hash=hashlib.sha256(self.resume.read_bytes()).hexdigest(), locations=['Worldwide'])
        self.enterContext(patch.object(apply, 'HOME', self.home))
        self.enterContext(patch.object(apply, 'LAST_RUN_FILE', self.home / 'last_run.txt'))

    def test_profile_summary_and_english_level_labels_are_english(self):
        output = io.StringIO()
        with redirect_stdout(output):
            apply.show_profile(self.prof, ['Worldwide'])
        for label in ['Profile extracted from your CV', 'Name:', 'City:', 'Current title:',
                      'Job titles to search:', 'Title keywords:', 'Countries / regions:', 'Cover letter:']:
            self.assertIn(label, output.getvalue())
        self.assertEqual([value[0] for value in apply.ENGLISH_LEVELS.values()],
                         ['Native / bilingual', 'Fluent / professional', 'Conversational / intermediate', 'Basic'])

    def test_terminal_profile_confirmation_defaults_to_english_yes(self):
        with patch('builtins.input', return_value='') as prompt, redirect_stdout(io.StringIO()), \
                patch.object(cv_profile, 'extract') as extract:
            apply.cv_setup()
        self.assertIn('Use this profile?', prompt.call_args.args[0])
        self.assertIn('[y]', prompt.call_args.args[0])
        self.assertEqual(apply.SETTINGS['cv_profile'], self.prof)
        extract.assert_not_called()

    def test_terminal_edit_titles_uses_english_action_and_prompt(self):
        with patch('builtins.input', side_effect=['t', 'Backend Engineer', 'y']) as prompt, redirect_stdout(io.StringIO()):
            apply.cv_setup()
        self.assertEqual(apply.SETTINGS['cv_profile']['job_titles'], ['Backend Engineer'])
        self.assertIn('Job titles (comma-separated)', prompt.call_args_list[1].args[0])

    def test_terminal_edit_locations_uses_english_action_and_prompt(self):
        with patch('builtins.input', side_effect=['l', 'United Kingdom, Canada', 'y']) as prompt, redirect_stdout(io.StringIO()):
            apply.cv_setup()
        self.assertEqual(apply.SETTINGS['locations'], ['United Kingdom', 'Canada'])
        self.assertIn('Countries / regions (comma-separated', prompt.call_args_list[1].args[0])

    def test_terminal_decline_uses_english_no(self):
        with patch('builtins.input', return_value='n'), redirect_stdout(io.StringIO()):
            apply.cv_setup()
        self.assertNotIn('cv_profile', apply.SETTINGS)

    def test_mocked_run_announces_mode_and_completion_in_english(self):
        apply.CFG['sources']['linkedin'] = False
        ctx = node()
        ctx.pages = [node()]
        for dry_run in [True, False]:
            with self.subTest(dry_run=dry_run), patch.object(apply, 'DRY_RUN', dry_run), \
                    patch.object(apply, 'load_applied', return_value={}), patch.object(apply, 'sync_playwright'), \
                    patch.object(apply, 'launch', return_value=ctx), patch.object(sources, 'run', return_value=0):
                apply.log.reset_mock()
                apply.run_bot()
                messages = [call.args[0] for call in apply.log.call_args_list]
                self.assertTrue(any(message.startswith('Starting (test mode' if dry_run else 'Starting (LIVE mode') for message in messages))
                self.assertTrue(any(message.startswith('Finished.') for message in messages))

    def test_chrome_launch_error_is_english(self):
        pw = MagicMock()
        pw.chromium.launch_persistent_context.side_effect = RuntimeError('fixture')
        with self.assertRaisesRegex(RuntimeError, 'Google Chrome could not be launched'):
            apply.launch(pw)

    def test_unreadable_cv_error_is_english_before_network(self):
        with patch.object(cv_profile, 'cv_text', return_value=''), patch.object(cv_profile, 'request') as request:
            with self.assertRaisesRegex(ValueError, 'Could not extract enough CV text'):
                cv_profile.extract(self.resume, 'fixture-key')
        request.assert_not_called()

    def test_builtin_output_literals_and_python_comments_do_not_use_turkish(self):
        turkish = re.compile('[ğĞıİşŞçÇöÖüÜ]')
        root = Path(apply.__file__).parent
        for filename in ['apply.py', 'gui.py', 'cv_profile.py', 'sources.py', 'llm_config.py', 'llm_credentials.py', 'job_metadata.py']:
            text = (root / filename).read_text(encoding='utf-8')
            tree = ast.parse(text)
            for token in tokenize.generate_tokens(io.StringIO(text).readline):
                if token.type == tokenize.COMMENT:
                    self.assertIsNone(turkish.search(token.string), (filename, token.start))
            for item in ast.walk(tree):
                if isinstance(item, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    self.assertIsNone(turkish.search(ast.get_docstring(item) or ''), (filename, getattr(item, 'lineno', 1)))
                if not isinstance(item, ast.Call):
                    continue
                name = item.func.id if isinstance(item.func, ast.Name) else item.func.attr if isinstance(item.func, ast.Attribute) else ''
                if name not in {'log', 'print', 'input', 'ask', 'wait_human', 'RuntimeError', 'ValueError', 'NoFreeModel'}:
                    continue
                for arg in item.args:
                    for literal in ast.walk(arg):
                        if isinstance(literal, ast.Constant) and isinstance(literal.value, str):
                            self.assertIsNone(turkish.search(literal.value), (filename, item.lineno))
