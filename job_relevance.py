"""Deterministic, conservative Rails/backend suitability; job text is data only."""
import re

VERSION = 'rails-backend-v3-user-regions'
TARGET_ROLES = [
    'Senior Ruby on Rails Developer', 'Senior Ruby on Rails Engineer', 'Senior Ruby Developer',
    'Lead Ruby on Rails Developer', 'Ruby on Rails Technical Lead', 'Senior Backend Engineer',
    'Senior Backend Developer', 'Lead Software Engineer', 'Senior Full Stack Developer',
]
RUBY = re.compile(r'\bruby\b|\brails\b|\bror\b', re.I)
SPECIALIZED = re.compile(r'\bios\b|\bandroid\b|\bdata engineer|\bdata scientist|\bsre\b|site reliability|kubernetes|\bdevops\b|cloud (?:software )?architect|machine learning|\bai infrastructure\b|\bgtm\b|database engineer', re.I)
GENERAL = re.compile(r'back[ -]?end|full[ -]?stack|(?:software|technical|tech)\s+(?:engineer|developer|lead|leader)|(?:lead|staff|senior|sr\.?|principal)\s+(?:software|engineer|developer)|desarrollador.*back', re.I)
INCOMPATIBLE = re.compile(r'\bpython\b|\bdjango\b|\bjava\b|\bspring\b|\bgolang\b|\bgo\b(?!\s+(?:to|above|beyond|the|through|live|fast)\b)|\bc#|\.net\b|\belixir\b|\brust\b|\bphp\b|\blaravel\b|\bnode(?:\.js|js)?\b', re.I)
REQUIRED = re.compile(r'\brequired\b|\bmust\b|\bmandatory\b|\bproficien|\bstrong (?:experience|knowledge)|\bexperience (?:with|in)|\bexpertise in|\b(?:build|develop|work).*\b(?:using|with|in)\b|requisito|obligatorio|experiencia (?:en|con)|experiência (?:em|com)', re.I)
OPTIONAL = re.compile(r'nice.to.have|preferred|\bbonus\b|\ba plus\b|optional|not required|not necessary|no .*required|deseable|diferencial|valorable', re.I)


def reason(title: str, description: str) -> str | None:
    if not title.strip():
        return 'identity_unknown'
    if SPECIALIZED.search(title):
        return 'specialized_role'
    explicit = bool(RUBY.search(title))
    if explicit and not re.search(r'developer|engineer|lead|leader', title, re.I):
        return 'title_not_relevant'
    if not explicit and INCOMPATIBLE.search(title):
        return 'required_stack_incompatible'
    if not explicit and not GENERAL.search(title):
        return 'title_not_relevant'
    required_section, optional_section, ruby_core = False, False, False
    for line in description.splitlines():
        text = line.strip()
        if re.fullmatch(r'((?:technical |minimum |essential )?(?:requirements|qualifications)|what you bring|what you need|must.have|requisitos(?: técnicos)?|required skills)[:\s]*', text, re.I):
            required_section, optional_section = True, False
            continue
        if re.fullmatch(r'(nice.to.have|preferred skills|benefits|responsibilities|what we offer|deseable)[:\s]*', text, re.I):
            required_section, optional_section = False, bool(re.search('nice|preferred|deseable', text, re.I))
            continue
        for clause in re.split(r';|(?<=[.!?])\s+|,\s*', text):
            if not clause or optional_section or OPTIONAL.search(clause):
                continue
            if RUBY.search(clause) and INCOMPATIBLE.search(clause) and re.search(r'migrat|moving away|replace.*ruby|legacy', clause, re.I):
                return 'stack_unknown'
            if INCOMPATIBLE.search(clause) and (required_section or REQUIRED.search(clause)):
                if RUBY.search(clause) and re.search(r'\bor\b|\bo\b|\bou\b|/', clause, re.I):
                    ruby_core = True
                    continue  # Ruby is an explicitly accepted alternative.
                return 'required_stack_incompatible'
            if RUBY.search(clause):
                ruby_core = True
    if explicit:
        return None
    if not description.strip():
        return 'stack_unknown'
    # A generic Engineer title or an incidental optional Rails mention is insufficient.
    if ruby_core:
        return None
    return 'stack_unknown'

