"""Technology filtering driven exclusively by user-reviewed GUI settings."""
import re

VERSION = 'user-filters-v2'
FIELDS = ('target_technologies', 'excluded_technologies')
USER_FIELDS = (*FIELDS, 'excluded_title_words')
REQUIRED = re.compile(r'\brequired\b|\bmust\b|\bmandatory\b|\bproficien|\bstrong (?:experience|knowledge)|\bexperience (?:with|in)|\bexpertise in|\b(?:build|develop|work).*\b(?:using|with|in)\b|requisito|obligatorio|experiencia (?:en|con)|experiência (?:em|com)', re.I)
OPTIONAL = re.compile(r'nice.to.have|preferred|\bbonus\b|\ba plus\b|optional|not required|not necessary|no .*required|deseable|diferencial|valorable', re.I)
REQUIRED_HEADING = re.compile(r'((?:technical |minimum |essential )?(?:requirements|qualifications)|what you bring|what you need|must.have|requisitos(?: técnicos)?|required skills)[:\s]*', re.I)
OPTIONAL_HEADING = re.compile(r'(nice.to.have|preferred(?: skills)?|optional(?: skills)?|deseable)[:\s]*', re.I)
OTHER_HEADING = re.compile(r'(benefits|responsibilities|what we offer|about (?:us|the company))[:\s]*', re.I)


def validate_settings(settings: dict) -> None:
    """Reject malformed lists and contradictory user rules before a run starts."""
    for field in USER_FIELDS:
        values = settings.get(field, [])
        if (not isinstance(values, list) or len(values) > 100
                or any(not isinstance(v, str) or not v.strip() or len(v) > 100
                       or any(ord(c) < 32 for c in v) for v in values)):
            raise ValueError(f'settings.{field}: expected up to 100 nonblank single-line terms (100 characters each)')
    targets = {v.strip().casefold() for v in settings.get('target_technologies', [])}
    excluded = {v.strip().casefold() for v in settings.get('excluded_technologies', [])}
    if targets & excluded:
        raise ValueError('A technology cannot be both targeted and excluded')


def matches(technology: str, text: str) -> bool:
    """Literal case-insensitive terms, not user regex; preserve C++, C#, .NET."""
    pattern = re.escape(technology.strip()).replace(r'\ ', r'\s+')
    return bool(re.search(rf'(?<![\w+#.]){pattern}(?![\w+#])', text, re.I))


def excluded_title_word(title: str, settings: dict) -> str | None:
    """Only explicit user terms, matched literally against the title, not body."""
    return next((term for term in settings.get('excluded_title_words', []) if matches(term, title)), None)


def assess(title: str, description: str, *, target_technologies=(), excluded_technologies=()) -> dict:
    """Return a reason and its evidence; no built-in language or role blacklist.

    Targets mean at least one selected technology in the title/core description.
    Exclusions apply to titles and mandatory requirements, never optional skills.
    Missing evidence is manual review, not proof of an incompatible skill set.
    """
    targets = list(target_technologies)
    excluded = list(excluded_technologies)
    for technology in excluded:
        if matches(technology, title):
            return {'reason': 'excluded_technology', 'technology': technology, 'evidence': title[:500], 'source': 'title'}
    target_seen = any(matches(tech, title) for tech in targets)
    required_section, optional_section = False, False
    for line in description.splitlines():
        text = line.strip().lstrip('•*- ').strip()
        if OPTIONAL_HEADING.fullmatch(text):
            required_section, optional_section = False, True
            continue
        if REQUIRED_HEADING.fullmatch(text):
            required_section, optional_section = True, False
            continue
        if OTHER_HEADING.fullmatch(text):
            required_section, optional_section = False, False
            continue
        heading, separator, rest = text.partition(':')
        if separator and REQUIRED_HEADING.fullmatch(heading):
            required_section, optional_section, text = True, False, rest.strip()
        elif separator and OPTIONAL_HEADING.fullmatch(heading):
            required_section, optional_section, text = False, True, rest.strip()
        # Do not split comma-separated alternatives or punctuation inside .NET,
        # Node.js etc.; a clause must retain its requirement/optional qualifier.
        for clause in re.split(r';|(?<=[.!?])\s+(?=[A-Z])', text):
            # Keep accepted alternatives together, but do not let "Ruby
            # preferred" hide "Python required" in a separate comma clause.
            alternatives = re.search(r'\bor\b|\bo\b|\bou\b', clause, re.I)
            parts = [clause] if alternatives and not OPTIONAL.search(clause) else re.split(r',\s*', clause)
            for part in parts:
                if not part or optional_section or OPTIONAL.search(part):
                    continue
                positive = any(matches(tech, part) for tech in targets)
                negative = next((tech for tech in excluded if matches(tech, part)), None)
                if negative and (required_section or REQUIRED.search(part)):
                    if positive and re.search(r'\bor\b|\bo\b|\bou\b', part, re.I):
                        # An explicitly accepted target alternative is not a conflict.
                        target_seen = True
                        continue
                    return {'reason': 'excluded_technology', 'technology': negative, 'evidence': part[:500], 'source': 'requirement'}
                target_seen = target_seen or positive
    if targets and not target_seen:
        return {'reason': 'stack_unknown', 'evidence': 'No selected target technology found in the title or non-optional job description'}
    return {'reason': None}


def reason(title: str, description: str, **settings) -> str | None:
    return assess(title, description, **settings)['reason']
