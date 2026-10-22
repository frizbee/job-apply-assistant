"""Explicit phone components; never infer a calling code from CV geography."""
import re


def split_phone(full: str) -> tuple[str, str]:
    """Only split a legacy number whose calling code is explicitly delimited."""
    match = re.fullmatch(r'\s*\+([1-9]\d{0,2})[\s-]+([\d\s().-]+)\s*', full)
    if not match:
        return '', ''
    code, number = '+' + match[1], re.sub(r'\D', '', match[2])
    try:
        join_phone(code, number)
    except ValueError:
        return '', ''
    return code, number


def join_phone(code: str, number: str) -> str:
    """Validate user-reviewed components and keep their boundary in storage."""
    if not isinstance(code, str) or not isinstance(number, str):
        raise ValueError('Phone country code and local number must be text')
    code, number = code.strip(), number.strip()
    if not re.fullmatch(r'\+[1-9]\d{0,2}', code):
        raise ValueError('Enter a country calling code such as +1 or +90')
    if not re.fullmatch(r'[\d\s().-]+', number) or not re.search(r'\d', number):
        raise ValueError('Enter the phone number without its country calling code')
    digits = re.sub(r'\D', '', number)
    if len(code) - 1 + len(digits) > 15:
        raise ValueError('The complete phone number must contain at most 15 digits')
    return code + ' ' + digits
