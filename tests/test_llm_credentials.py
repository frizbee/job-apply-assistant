"""Provider isolation, safe .env persistence and environment precedence; no network."""
import os
from pathlib import Path
import stat
import tempfile
import threading
import unittest
from unittest.mock import patch

import llm_credentials as credentials


class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory(prefix='llm-credentials-')))
        self.path = self.home / '.env'

    def write(self, text):
        self.path.write_text(text, encoding='utf-8')

    def load(self, provider='openrouter', **kwargs):
        return credentials.load(self.home, provider, environ={}, **kwargs)

    def test_each_provider_only_loads_its_own_key(self):
        self.write('OPENROUTER_API_KEY=router-key\nOPENAI_API_KEY=openai-key\nANTHROPIC_API_KEY=claude-key\n')
        for provider, value in [('openrouter', 'router-key'), ('openai', 'openai-key'), ('anthropic', 'claude-key')]:
            self.assertEqual(self.load(provider).value, value)
            self.assertEqual(self.load(provider).source, 'dotenv')

    def test_environment_has_precedence_without_reading_dotenv(self):
        self.path.write_bytes(b'\xff')
        self.assertEqual(credentials.load(self.home, 'openai', environ={'OPENAI_API_KEY': 'environment-key'}).value, 'environment-key')

    def test_explicit_key_overrides_environment_and_blank_uses_saved_key(self):
        env = {'OPENAI_API_KEY': 'environment-key'}
        self.assertEqual(credentials.load(self.home, 'openai', environ=env, explicit_key='input-key').source, 'input')
        self.assertEqual(credentials.load(self.home, 'openai', environ=env, explicit_key='').value, 'environment-key')

    def test_missing_and_empty_template_values_are_not_credentials(self):
        self.assertEqual(self.load().public()['source'], 'missing')
        for value in ['', '""', "''", '# placeholder', '"" # placeholder']:
            self.write(f'OPENAI_API_KEY={value}\n')
            self.assertFalse(self.load('openai').public()['has_key'])

    def test_export_spacing_quotes_and_comments_are_supported(self):
        for line in [' export OPENAI_API_KEY = "test-key" # comment', "OPENAI_API_KEY='test-key'", 'OPENAI_API_KEY=test-key # comment']:
            self.write(line + '\n')
            self.assertEqual(self.load('openai').value, 'test-key')

    def test_invalid_tokens_and_header_injection_do_not_echo_secrets(self):
        for key in ['PRIVATE-SECRET\r\nHeader: injected', 'PRIVATE-SECRET\t', 'PRIVATE SECRET', '${PRIVATE-SECRET}', '秘密', 'a' * 4097]:
            with self.assertRaises(credentials.CredentialError) as caught:
                credentials.validate_key(key)
            self.assertNotIn('PRIVATE-SECRET', str(caught.exception))

    def test_duplicate_selected_assignments_fail_closed(self):
        self.write('OPENAI_API_KEY=first\nexport OPENAI_API_KEY=second\nOPENROUTER_API_KEY=router\n')
        with self.assertRaises(credentials.CredentialError):
            self.load('openai')
        self.assertEqual(self.load().value, 'router')

    def test_unmatched_quote_and_invalid_utf8_fail_closed(self):
        self.write('OPENAI_API_KEY="PRIVATE-SECRET\n')
        with self.assertRaises(credentials.CredentialError):
            self.load('openai')
        self.path.write_bytes(b'\xff')
        with self.assertRaises(credentials.CredentialError):
            self.load('openai')

    def test_public_metadata_and_repr_never_contain_key(self):
        self.write('OPENAI_API_KEY=PRIVATE-SECRET\n')
        key = self.load('openai')
        self.assertNotIn('PRIVATE-SECRET', repr(key))
        self.assertEqual(key.public(), {'provider': 'openai', 'has_key': True, 'source': 'dotenv'})

    def test_save_and_forget_preserve_other_providers_and_unicode_lines(self):
        self.write('# комментарий 日本語\nOTHER=значение\nOPENROUTER_API_KEY=router\nexport OPENAI_API_KEY = old\n')
        credentials.save(self.home, 'openai', 'new-key')
        self.assertEqual(self.load('openai').value, 'new-key')
        self.assertIn('OTHER=значение', self.path.read_text(encoding='utf-8'))
        self.assertEqual(self.load().value, 'router')
        credentials.forget(self.home, 'openai')
        self.assertFalse(self.load('openai').value)
        self.assertEqual(self.load().value, 'router')

    def test_forget_does_not_remove_an_environment_key(self):
        self.write('OPENAI_API_KEY=stored-key\n')
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'environment-key'}):
            credentials.forget(self.home, 'openai')
            self.assertEqual(credentials.load(self.home, 'openai').source, 'environment')

    def test_failed_replace_preserves_original_and_cleans_temporary_file(self):
        self.write('OPENAI_API_KEY=old-key\n')
        before = self.path.read_bytes()
        with patch.object(os, 'replace', side_effect=OSError('disk error')), self.assertRaises(credentials.CredentialError):
            credentials.save(self.home, 'openai', 'new-key')
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.home.glob('.env.*')), [])

    def test_saved_file_is_private_on_posix(self):
        self.write('OTHER=keep\n')
        if os.name == 'posix':
            self.path.chmod(0o644)
        credentials.save(self.home, 'anthropic', 'test-key')
        if os.name == 'posix':
            self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_concurrent_provider_writes_do_not_lose_other_keys(self):
        barrier = threading.Barrier(3)
        errors = []
        def save(provider):
            try:
                barrier.wait(timeout=5)
                credentials.save(self.home, provider, provider + '-key')
            except BaseException as error:
                errors.append(error)
        threads = [threading.Thread(target=save, args=(provider,), daemon=True) for provider in ['openai', 'anthropic']]
        for thread in threads:
            thread.start()
        barrier.wait(timeout=5)
        for thread in threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.load('openai').value, 'openai-key')
        self.assertEqual(self.load('anthropic').value, 'anthropic-key')

    def test_symlinked_file_is_not_replaced(self):
        target = self.home / 'target'
        target.write_text('unchanged', encoding='utf-8')
        try:
            self.path.symlink_to(target)
        except OSError:
            self.skipTest('Symlinks are unavailable in this environment')
        with self.assertRaises(credentials.CredentialError):
            credentials.save(self.home, 'openai', 'test-key')
        self.assertEqual(target.read_text(encoding='utf-8'), 'unchanged')

    def test_invalid_provider_and_save_values_do_not_touch_disk(self):
        for provider, key in [('unknown', 'test-key'), ('openai', ''), ('openai', 'bad\nkey')]:
            with self.assertRaises(credentials.CredentialError):
                credentials.save(self.home, provider, key)
        self.assertFalse(self.path.exists())
