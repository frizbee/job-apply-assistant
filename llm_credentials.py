"""Separate provider credentials; .env is plaintext, never executable shell code."""
from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import tempfile
import threading

KEY_NAMES = {'openrouter': 'OPENROUTER_API_KEY', 'openai': 'OPENAI_API_KEY', 'anthropic': 'ANTHROPIC_API_KEY'}
WRITE_LOCK = threading.Lock()


class CredentialError(ValueError):
    """Secret-free errors suitable for application boundaries."""


@dataclass(frozen=True)
class Credential:
    provider: str
    value: str = field(repr=False)
    source: str = 'missing'

    def public(self) -> dict:
        return {'provider': self.provider, 'has_key': bool(self.value), 'source': self.source}


def _name(provider: str) -> str:
    if not isinstance(provider, str) or provider not in KEY_NAMES:
        raise CredentialError('Unsupported credential provider')
    return KEY_NAMES[provider]


def validate_key(value: str) -> str:
    # No provider-prefix heuristics, interpolation, quotes or header injection.
    if not isinstance(value, str):
        raise CredentialError('API key must be a single ASCII token')
    value = value.strip(' ')
    if not re.fullmatch(r'[A-Za-z0-9._:/+=-]{1,4096}', value):
        raise CredentialError('API key must be a single ASCII token of at most 4096 characters')
    return value


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding='utf-8')
    except FileNotFoundError:
        return ''
    except (OSError, UnicodeError):
        raise CredentialError('Cannot read UTF-8 credentials file') from None


def _assignment(name: str):
    return re.compile(r'^\s*(?:export\s+)?' + re.escape(name) + r'\s*=(.*)$')


def load(home: Path, provider: str = 'openrouter', *, environ=None, explicit_key: str | None = None) -> Credential:
    name = _name(provider)
    if explicit_key is not None:
        if not isinstance(explicit_key, str):
            raise CredentialError('API key must be a single ASCII token')
        if explicit_key.strip(' '):
            return Credential(provider, validate_key(explicit_key), 'input')
    environment = os.environ if environ is None else environ
    if environment.get(name):
        return Credential(provider, validate_key(environment[name]), 'environment')
    pattern = _assignment(name)
    values = [match[1].strip() for line in _read(Path(home) / '.env').splitlines() if (match := pattern.fullmatch(line))]
    if len(values) > 1:
        raise CredentialError('Duplicate provider key assignments in credentials file')
    if not values:
        return Credential(provider, '')
    value = values[0]
    if value.startswith(('"', "'")):
        match = re.fullmatch(r'''(["'])(.*?)\1(?:\s+\#.*)?''', value)
        if not match:
            raise CredentialError('Malformed quoted provider key in credentials file')
        value = match[2]
    else:
        value = '' if value.startswith('#') else re.split(r'\s+#', value, maxsplit=1)[0].strip()
    if not value:
        return Credential(provider, '')
    return Credential(provider, validate_key(value), 'dotenv')


def _replace(home: Path, name: str, value: str | None) -> None:
    path = Path(home) / '.env'
    with WRITE_LOCK:
        if path.is_symlink():
            raise CredentialError('Refusing to replace a symlinked credentials file')
        original = _read(path)
        lines = [line for line in original.splitlines(keepends=True) if not _assignment(name).fullmatch(line.rstrip('\r\n'))]
        if value is None and ''.join(lines) == original:
            return
        text = ''.join(lines)
        if value is not None:
            text += ('\n' if text and not text.endswith('\n') else '') + f'{name}={value}\n'
        temporary = None
        try:
            fd, temporary = tempfile.mkstemp(prefix='.env.', dir=home)
            with os.fdopen(fd, 'w', encoding='utf-8', newline='') as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except OSError:
            raise CredentialError('Cannot atomically update credentials file') from None
        finally:
            if temporary is not None:
                try:
                    Path(temporary).unlink(missing_ok=True)
                except OSError:
                    pass


def save(home: Path, provider: str, value: str) -> None:
    """Storage primitive: callers must check provider authorization before saving."""
    name = _name(provider)
    value = validate_key(value)
    _replace(Path(home), name, value)


def forget(home: Path, provider: str) -> None:
    _replace(Path(home), _name(provider), None)
