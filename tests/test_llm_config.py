"""Offline validation, precedence and snapshots for the staged LLM profiles."""
import copy
from dataclasses import FrozenInstanceError
import unittest

import llm_config


def configuration():
    return {'llm': {'default_profile': 'free', 'profiles': {
        'free': {'provider': 'openrouter', 'selection': 'free_auto', 'cost_policy': 'free_only'},
        'openai': {'provider': 'openai', 'selection': 'explicit', 'model': 'test-openai-model', 'cost_policy': 'paid_allowed'},
        'claude': {'provider': 'anthropic', 'selection': 'explicit', 'model': None, 'cost_policy': 'paid_allowed'},
    }}}


class LLMConfigTests(unittest.TestCase):
    def test_legacy_config_defaults_to_free_without_mutation(self):
        original = {'search': {}}
        resolved = llm_config.resolve(original)
        self.assertEqual((resolved.profile, resolved.provider, resolved.selection), ('free', 'openrouter', 'free_auto'))
        self.assertEqual(resolved.max_output_tokens, 4096)
        self.assertEqual(original, {'search': {}})

    def test_default_profile_from_config(self):
        config = configuration()
        config['llm']['default_profile'] = 'openai'
        resolved = llm_config.resolve(config)
        self.assertEqual(resolved.model, 'test-openai-model')
        self.assertFalse(resolved.legacy_supported)

    def test_saved_and_request_selection_and_limits_have_ordered_precedence(self):
        config = configuration()
        saved = {'profile': 'openai', 'overrides': {'openai': {'model': 'saved-model', 'max_output_tokens': 1024}}}
        resolved = llm_config.resolve(config, saved, {'model': 'requested-model', 'timeout_seconds': 30})
        self.assertEqual((resolved.model, resolved.max_output_tokens, resolved.timeout_seconds), ('requested-model', 1024, 30))
        self.assertEqual(llm_config.resolve(config, saved).model, 'saved-model')

    def test_overrides_do_not_leak_between_profiles(self):
        saved = {'profile': 'openai', 'overrides': {'openai': {'model': 'saved-model', 'max_output_tokens': 512}}}
        resolved = llm_config.resolve(configuration(), saved, {'profile': 'claude', 'model': 'test-claude-model'})
        self.assertEqual(resolved.provider, 'anthropic')
        self.assertEqual(resolved.max_output_tokens, 4096)

    def test_incomplete_profile_can_be_listed_but_not_run(self):
        config = configuration()
        result = llm_config.resolve(config, {'profile': 'claude'}, require_model=False)
        self.assertFalse(result.ready)
        with self.assertRaises(llm_config.LLMConfigError):
            llm_config.resolve(config, {'profile': 'claude'})

    def test_explicit_openrouter_does_not_use_legacy_adapter(self):
        config = configuration()
        config['llm']['profiles']['router-paid'] = {'provider': 'openrouter', 'selection': 'explicit',
            'model': 'vendor/test-model', 'cost_policy': 'paid_allowed'}
        self.assertFalse(llm_config.resolve(config, {'profile': 'router-paid'}).legacy_supported)

    def test_invalid_explicit_sections_never_fall_back_to_free(self):
        for section in [None, {}, [], {'default_profile': 'free', 'profiles': {}},
                        {'default_profile': 'missing', 'profiles': configuration()['llm']['profiles']},
                        {**configuration()['llm'], 'base_url': 'https://example.test'}]:
            with self.subTest(section=section), self.assertRaises(llm_config.LLMConfigError):
                llm_config.resolve({'llm': section})

    def test_invalid_fields_types_and_combinations_are_rejected(self):
        cases = [('provider', 'unknown'), ('provider', []), ('selection', 'auto'), ('selection', False),
                 ('cost_policy', 'unknown'), ('cost_policy', []), ('model', ''), ('model', 'with spaces'),
                 ('model', 'https://evil.test/?secret'), ('max_output_tokens', True), ('max_output_tokens', 4097),
                 ('max_output_tokens', 255), ('max_output_tokens', 1000.0), ('timeout_seconds', '30'),
                 ('timeout_seconds', 9), ('timeout_seconds', 181), ('api_key', 'PRIVATE-SECRET')]
        for field, value in cases:
            config = configuration()
            config['llm']['profiles']['openai'][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(llm_config.LLMConfigError) as caught:
                llm_config.resolve(config)
            self.assertNotIn('PRIVATE-SECRET', str(caught.exception))

    def test_invalid_auto_and_native_free_policies_are_rejected(self):
        for values in [{'model': 'vendor/model'}, {'cost_policy': 'paid_allowed'}, {'provider': 'openai'}]:
            config = configuration()
            config['llm']['profiles']['free'].update(values)
            with self.assertRaises(llm_config.LLMConfigError):
                llm_config.resolve(config)
        config = configuration()
        config['llm']['profiles']['openai']['cost_policy'] = 'free_only'
        with self.assertRaises(llm_config.LLMConfigError):
            llm_config.resolve(config)

    def test_invalid_saved_preferences_and_request_overrides_are_rejected(self):
        for saved in [None, [], {'profile': 'missing'}, {'profile': True}, {'api_key': 'PRIVATE-SECRET'},
                      {'overrides': []}, {'overrides': {'missing': {}}},
                      {'overrides': {'free': {'model': 'paid'}}},
                      {'overrides': {'openai': {'provider': 'anthropic'}}}]:
            with self.subTest(saved=saved), self.assertRaises(llm_config.LLMConfigError):
                llm_config.resolve(configuration(), saved)
        for override in [None, [], {'profile': 'missing'}, {'paid_confirmed': True}, {'base_url': 'x'},
                         {'provider': 'anthropic'}, {'max_output_tokens': 99999}]:
            with self.subTest(override=override), self.assertRaises(llm_config.LLMConfigError):
                llm_config.resolve(configuration(), {}, override)

    def test_snapshot_is_immutable_stable_and_secret_free(self):
        config = configuration()
        before = copy.deepcopy(config)
        resolved = llm_config.resolve(config)
        self.assertEqual(resolved.snapshot_id, llm_config.resolve(config).snapshot_id)
        self.assertNotEqual(resolved.snapshot_id, llm_config.resolve(config, {}, {'max_output_tokens': 512}).snapshot_id)
        with self.assertRaises(FrozenInstanceError):
            resolved.model = 'changed'
        public = resolved.public()
        public['provider'] = 'changed'
        self.assertEqual(resolved.provider, 'openrouter')
        self.assertEqual(config, before)
        self.assertNotIn('api_key', resolved.public())

    def test_duplicate_yaml_fields_are_not_silently_overwritten(self):
        for text in ['llm: {}\nllm: {}', 'llm:\n  profiles:\n    free: {}\n    free: {}',
                     'llm:\n  profiles:\n    free:\n      provider: openrouter\n      provider: openai']:
            with self.assertRaisesRegex(llm_config.LLMConfigError, 'duplicate'):
                llm_config.load_yaml(text)

    def test_invalid_yaml_never_echoes_contents(self):
        with self.assertRaises(llm_config.LLMConfigError) as caught:
            llm_config.load_yaml('key: [PRIVATE-SECRET')
        self.assertNotIn('PRIVATE-SECRET', str(caught.exception))
