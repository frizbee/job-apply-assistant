"""Staged config/key integration must never route a native key to OpenRouter."""
import copy
import json
import os
from pathlib import Path
import tempfile
import threading
from unittest.mock import patch

from test_answer_safety import SafetyTest
from test_llm_config import configuration
import apply
import cv_profile
import gui
import llm_config
import llm_credentials


class LLMIntegrationTests(SafetyTest):
    def setUp(self):
        super().setUp()
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory(prefix='llm-integration-')))
        self.resume = self.home / 'resume.pdf'
        self.resume.write_bytes(b'fixture')
        apply.SETTINGS.update(resume=str(self.resume), notice='14')
        apply.CFG['llm'] = configuration()['llm']
        self.enterContext(patch.object(apply, 'HOME', self.home))
        self.enterContext(patch.object(apply, 'SETTINGS_FILE', self.home / 'settings.json'))
        self.enterContext(patch.dict(os.environ, {name: '' for name in llm_credentials.KEY_NAMES.values()}))
        self.enterContext(patch.object(gui, 'STATE', {'running': False, 'analyzing': False}))
        self.enterContext(patch.object(gui, 'ACTIVITY_LOCK', threading.Lock()))

    def test_legacy_wrappers_preserve_openrouter_default_and_isolate_native_keys(self):
        apply.save_key('router-key')
        apply.save_key('openai-key', 'openai')
        apply.save_key('claude-key', 'anthropic')
        self.assertEqual(apply.find_key(), 'router-key')
        self.assertEqual(apply.find_key('openai'), 'openai-key')
        self.assertEqual(apply.find_key('anthropic'), 'claude-key')

    def test_state_reports_selected_profile_and_only_safe_credential_metadata(self):
        apply.SETTINGS['llm_preferences'] = {'profile': 'openai'}
        apply.save_key('PRIVATE-SECRET', 'openai')
        result = gui.state()
        self.assertEqual(result['llm']['profile'], 'openai')
        self.assertFalse(result['llm']['available'])
        self.assertTrue(result['has_key'])
        self.assertEqual(result['credentials']['openai']['source'], 'dotenv')
        self.assertNotIn('PRIVATE-SECRET', json.dumps(result))

    def test_native_or_explicit_profile_cannot_fall_back_or_contact_openrouter(self):
        for name in ['openai', 'claude']:
            apply.SETTINGS['llm_preferences'] = {'profile': name, 'overrides': {name: {'model': 'test-model'}}}
            with patch.object(cv_profile, 'request') as request, patch.object(cv_profile, 'extract') as extract:
                self.assertEqual(gui.analyze({'key': 'PRIVATE-SECRET'})['error'], 'llm_not_implemented')
            request.assert_not_called()
            extract.assert_not_called()
            self.assertFalse(gui.ACTIVITY_LOCK.locked())

    def test_incomplete_selected_profile_is_a_configuration_error(self):
        apply.SETTINGS['llm_preferences'] = {'profile': 'claude'}
        with patch.object(cv_profile, 'request') as request:
            self.assertEqual(gui.analyze({'key': ''})['error'], 'invalid_llm_config')
        request.assert_not_called()

    def test_invalid_config_and_credentials_are_safe_errors_not_fallbacks(self):
        apply.CFG['llm']['profiles']['free']['api_key'] = 'PRIVATE-SECRET'
        result = gui.analyze({'key': ''})
        self.assertEqual(result['error'], 'invalid_llm_config')
        self.assertNotIn('PRIVATE-SECRET', json.dumps(result))
        apply.CFG['llm'] = configuration()['llm']
        (self.home / '.env').write_text('OPENROUTER_API_KEY="PRIVATE-SECRET\n', encoding='utf-8')
        result = gui.analyze({'key': ''})
        self.assertEqual(result['error'], 'invalid_key_config')
        self.assertNotIn('PRIVATE-SECRET', json.dumps(result))

    def test_current_profile_limits_are_passed_to_existing_free_adapter(self):
        apply.save_key('test-key')
        apply.SETTINGS['llm_preferences'] = {'overrides': {'free': {'max_output_tokens': 1024, 'timeout_seconds': 30}}}
        prof = cv_profile.empty_profile()
        prof['job_titles'] = ['Engineer']
        with patch.object(cv_profile, 'extract', return_value=prof) as extract:
            self.assertTrue(gui.analyze({'key': ''})['ok'])
        self.assertEqual(extract.call_args.kwargs['max_output_tokens'], 1024)
        self.assertEqual(extract.call_args.kwargs['timeout_seconds'], 30)

    def test_explicit_gui_key_does_not_silently_use_environment_key(self):
        key = 'sk-or-' + 'x' * 25
        prof = cv_profile.empty_profile()
        prof['job_titles'] = ['Engineer']
        with patch.dict(os.environ, {'OPENROUTER_API_KEY': 'environment-key'}), \
                patch.object(cv_profile, 'request'), patch.object(cv_profile, 'extract', return_value=prof) as extract:
            self.assertTrue(gui.analyze({'key': key})['ok'])
        self.assertEqual(extract.call_args.args[1], key)

    def test_invalid_saved_preferences_do_not_mutate_memory(self):
        before = copy.deepcopy(apply.SETTINGS)
        apply.SETTINGS_FILE.write_text(json.dumps({'llm_preferences': {'api_key': 'PRIVATE-SECRET'}}), encoding='utf-8')
        with self.assertRaises(llm_config.LLMConfigError):
            apply.load_settings()
        self.assertEqual(apply.SETTINGS, before)

    def test_invalid_preferences_are_not_saved(self):
        apply.SETTINGS_FILE.write_text('original', encoding='utf-8')
        apply.SETTINGS['llm_preferences'] = {'profile': 'missing'}
        with self.assertRaises(llm_config.LLMConfigError):
            apply.finalize_settings()
        self.assertEqual(apply.SETTINGS_FILE.read_text(encoding='utf-8'), 'original')

    def test_terminal_does_not_send_native_profile_to_free_extractor(self):
        apply.SETTINGS['llm_preferences'] = {'profile': 'openai'}
        with patch.object(cv_profile, 'extract') as extract, self.assertRaises(llm_config.LLMConfigError):
            apply.cv_setup()
        extract.assert_not_called()

    def test_free_generation_body_and_http_timeout_use_resolved_limits(self):
        prof = cv_profile.empty_profile()
        prof['job_titles'] = ['Engineer']
        response = {'choices': [{'message': {'content': json.dumps(prof)}}]}
        with patch.object(cv_profile, 'cv_text', return_value='fixture ' * 50), \
                patch.object(cv_profile, 'free_models', return_value=[('test:free', True)]), \
                patch.object(cv_profile, 'request', return_value=response) as request:
            self.assertEqual(cv_profile.extract(self.resume, 'test-key', max_output_tokens=1024, timeout_seconds=30), prof)
        self.assertEqual(request.call_args.args[2]['max_tokens'], 1024)
        self.assertEqual(request.call_args.kwargs['timeout_seconds'], 30)
