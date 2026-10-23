"""Reviewed contact data and exact question templates, never a default Yes."""
import re


TEMPLATES = [
    'What is your nationality?',
    'Do you have authorization to live and work in your country?',
    'Will you require visa sponsorship?',
    'Are you ok to be available for periodic on-call shifts?',
    'What is one thing that you worked on recently that you are incredibly proud of?',
    'Why do you want to work with us?',
    'Do you know someone that works at this company?',
    'Do you have any accessibility needs?',
    'Do you identify as a neurodivergent or neurodiverse person?',
    'Are you willing to relocate?',
]


def question_key(question):
    return ' '.join(question.strip().rstrip('*').strip().split()).casefold()


def validate_presets(entries):
    if not isinstance(entries, list) or len(entries) > 10:
        raise ValueError('question_presets: expected up to 10 question/answer pairs')
    seen = set()
    for entry in entries:
        if (not isinstance(entry, dict) or set(entry) != {'question', 'answer'}
                or not isinstance(entry['question'], str) or not isinstance(entry['answer'], str)
                or not entry['question'].strip() or len(entry['question']) > 2000
                or len(entry['answer']) > 10000):
            raise ValueError('question_presets: invalid question/answer pair')
        key = question_key(entry['question'])
        if not key or key in seen:
            raise ValueError('question_presets: duplicate or blank question')
        seen.add(key)


def preset_answer(question, settings):
    entries = settings.get('question_presets', [])
    if not settings.get('cv_hash') or settings.get('question_presets_cv_hash') != settings['cv_hash']:
        return []
    validate_presets(entries)
    match = next((e['answer'].strip() for e in entries if question_key(e['question']) == question_key(question)), '')
    return [match] if match else []


def contact_kind(question):
    key = question_key(question)
    patterns = [
        ('country_code', r'(?:phone )?country (?:calling )?code'),
        ('first_name', r'(?:first|given) name'),
        ('last_name', r'(?:last|family) name|surname'),
        ('full_name', r'(?:full|your) name'),
        ('email', r'e-?mail(?: address)?'),
        ('phone', r'(?:(?:mobile|contact|cell|telephone) )?(?:phone|telephone|mobile)(?: number)?'),
    ]
    return next((field for field, pattern in patterns if re.fullmatch(pattern, key)), None)


def contact_answers(question, profile):
    field = contact_kind(question)
    value = profile.get(field, '') if field else ''
    return [value] if value else []


def country_code_option(candidates, options):
    codes = {re.sub(r'[^\d]', '', value) for value in candidates if re.fullmatch(r'\(?\+\d{1,3}\)?', value)}
    matches = [i for i, option in enumerate(options)
               if len(found := re.findall(r'\+(\d{1,3})(?!\d)', option)) == 1 and found[0] in codes]
    # Shared calling codes (e.g. US/Canada +1) are ambiguous: never use residence.
    return matches[0] if len(matches) == 1 else None
