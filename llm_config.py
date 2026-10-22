"""Strict, secret-free LLM profiles and immutable per-operation configuration."""
from dataclasses import asdict, dataclass, replace
import hashlib
import json
import re
import yaml

PROVIDERS = frozenset({'openrouter', 'openai', 'anthropic'})
PROFILE_FIELDS = frozenset({'provider', 'selection', 'model', 'cost_policy', 'max_output_tokens', 'timeout_seconds'})
OVERRIDE_FIELDS = frozenset({'model', 'max_output_tokens', 'timeout_seconds'})
_UNSET = object()


class LLMConfigError(ValueError):
    """Messages describe a field/rule, never echo untrusted values or secrets."""


class _UniqueLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        self.flatten_mapping(node)
        mapping = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                if key in mapping:
                    raise LLMConfigError('config: duplicate YAML field')
                mapping[key] = self.construct_object(value_node, deep=deep)
            except TypeError:
                raise LLMConfigError('config: invalid YAML field') from None
        return mapping


def load_yaml(text: str) -> dict:
    try:
        result = yaml.load(text, Loader=_UniqueLoader)
    except yaml.YAMLError:
        raise LLMConfigError('config: invalid YAML') from None
    if not isinstance(result, dict):
        raise LLMConfigError('config: expected object')
    return result


@dataclass(frozen=True)
class ResolvedLLMConfig:
    profile: str
    provider: str
    selection: str
    model: str | None
    cost_policy: str
    max_output_tokens: int = 4096
    timeout_seconds: int = 180

    @property
    def ready(self) -> bool:
        return self.selection == 'free_auto' or self.model is not None

    @property
    def legacy_supported(self) -> bool:
        # No native/paid request may silently use the existing free-only adapter.
        return self.provider == 'openrouter' and self.selection == 'free_auto' and self.cost_policy == 'free_only'

    @property
    def snapshot_id(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode('utf-8')).hexdigest()

    def public(self) -> dict:
        return {**asdict(self), 'ready': self.ready, 'snapshot_id': self.snapshot_id}


def _object(value, fields, path):
    if not isinstance(value, dict) or any(key not in fields for key in value):
        raise LLMConfigError(f'{path}: expected an object with supported fields only')
    return value


def _name(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', value):
        raise LLMConfigError('llm.profile: expected a 1-64 character identifier')
    return value


def _validate(config: ResolvedLLMConfig) -> ResolvedLLMConfig:
    if not isinstance(config.provider, str) or config.provider not in PROVIDERS:
        raise LLMConfigError('llm.provider: unsupported provider')
    if config.selection not in ('free_auto', 'explicit'):
        raise LLMConfigError('llm.selection: unsupported selection mode')
    if config.cost_policy not in ('free_only', 'paid_allowed'):
        raise LLMConfigError('llm.cost_policy: unsupported cost policy')
    if config.model is not None and (not isinstance(config.model, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}', config.model)):
        raise LLMConfigError('llm.model: expected an exact model identifier or null')
    if config.selection == 'free_auto' and (config.provider != 'openrouter' or config.model is not None or config.cost_policy != 'free_only'):
        raise LLMConfigError('llm.selection: free_auto requires OpenRouter, no model and free_only')
    if config.provider != 'openrouter' and config.cost_policy != 'paid_allowed':
        raise LLMConfigError('llm.cost_policy: native API profiles must allow potentially paid requests')
    for field, low, high in [('max_output_tokens', 256, 4096), ('timeout_seconds', 10, 180)]:
        value = getattr(config, field)
        if type(value) is not int or not low <= value <= high:
            raise LLMConfigError(f'llm.{field}: expected an integer from {low} to {high}')
    return config


def profiles(config: dict) -> tuple[str, dict[str, ResolvedLLMConfig]]:
    """Absent legacy section migrates to free; an explicitly malformed one fails."""
    if not isinstance(config, dict):
        raise LLMConfigError('config: expected object')
    if 'llm' not in config:
        return 'free', {'free': ResolvedLLMConfig('free', 'openrouter', 'free_auto', None, 'free_only')}
    section = _object(config['llm'], {'default_profile', 'profiles'}, 'llm')
    raw_profiles = section.get('profiles')
    if not isinstance(raw_profiles, dict) or not 1 <= len(raw_profiles) <= 32:
        raise LLMConfigError('llm.profiles: expected 1-32 named profiles')
    result = {}
    for name, raw in raw_profiles.items():
        _name(name)
        raw = _object(raw, PROFILE_FIELDS, 'llm.profiles')
        if not {'provider', 'selection', 'cost_policy'} <= raw.keys():
            raise LLMConfigError('llm.profiles: provider, selection and cost_policy are required')
        result[name] = _validate(ResolvedLLMConfig(profile=name, **{'model': None, **raw}))
    default = _name(section.get('default_profile'))
    if default not in result:
        raise LLMConfigError('llm.default_profile: unknown profile')
    return default, result


def resolve(config: dict, preferences=_UNSET, overrides=_UNSET, *, require_model: bool = True) -> ResolvedLLMConfig:
    """config < saved, profile-scoped preferences < this call's explicit overrides."""
    default, choices = profiles(config)
    preferences = {} if preferences is _UNSET else _object(preferences, {'profile', 'overrides'}, 'llm_preferences')
    stored = preferences.get('overrides', {})
    if not isinstance(stored, dict):
        raise LLMConfigError('llm_preferences.overrides: expected object')
    for name, values in stored.items():
        _name(name)
        if name not in choices:
            raise LLMConfigError('llm_preferences.overrides: unknown profile')
        _object(values, OVERRIDE_FIELDS, 'llm_preferences.overrides')
        choices[name] = _validate(replace(choices[name], **values))
    selected = _name(preferences.get('profile', default))
    if selected not in choices:
        raise LLMConfigError('llm_preferences.profile: unknown profile')
    overrides = {} if overrides is _UNSET else _object(overrides, {'profile', *OVERRIDE_FIELDS}, 'llm_overrides')
    selected = _name(overrides.get('profile', selected))
    if selected not in choices:
        raise LLMConfigError('llm_overrides.profile: unknown profile')
    resolved = _validate(replace(choices[selected], **{key: value for key, value in overrides.items() if key != 'profile'}))
    if require_model and not resolved.ready:
        raise LLMConfigError('llm.model: choose an explicit model before analysis')
    return resolved
