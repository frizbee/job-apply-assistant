"""Conservative, offline detection of employer application-writing instructions.

Description text is untrusted evidence, never executable instructions or answers.
Matches require human review; quoted text is not automatically inserted anywhere.
"""
import re


ACTION = r'\b(?:include|mention|write|type|insert|add|put|use|enter|state|quote|copy|paste|contain|contains|containing|start|begin|end|finish|open|close|prefix|append)\b'
MARKER = r'\b(?:words?|phrases?|keywords?|code\s*words?|passwords?|secret\s+codes?|sentences?|statements?|strings?|tokens?|(?:following|this|exact)\s+text|subject\s+lines?)\b'
TARGET = r'\b(?:cover[ -]?letters?|covering\s+letters?|letters?\s+of\s+(?:application|motivation)|applications?|responses?|answers?|replies|reply|messages?|emails?|e-mails?|subject(?:\s+lines?)?|introductory\s+notes?)\b'
QUOTE = r'''["“‘'][^"”’'\n]{1,160}["”’']'''
CLAUSE = r'[^.!?]{0,180}?'
PATTERNS = tuple(re.compile(pattern, re.I) for pattern in (
    # Explicit phrase/keyword requirements, in either order.
    ACTION + CLAUSE + MARKER + CLAUSE + TARGET,
    TARGET + CLAUSE + ACTION + CLAUSE + MARKER,
    # "Include 'blue sky' in your letter" / "Your answer must contain '...'."
    ACTION + CLAUSE + QUOTE + CLAUSE + TARGET,
    TARGET + CLAUSE + ACTION + CLAUSE + QUOTE,
    # Opening/closing instructions may use an unquoted literal.
    r'\b(?:start|begin|end|finish|open|close|prefix)\b' + CLAUSE + TARGET + r'\s+(?:off\s+)?(?:with|by\s+(?:saying|writing))\b',
    TARGET + r'\s+(?:(?:must|should|needs?\s+to)\s+)?(?:start|begin|end|finish|open|close)\s+with\b',
    ACTION + CLAUSE + r'\b(?:at\s+the\s+(?:top|bottom|beginning|end)|as\s+the\s+(?:first|last)\s+(?:word|line|sentence))\b' + CLAUSE + TARGET,
    # Email/subject literals, including unquoted subject lines.
    r'\b(?:subject\s+line|email\s+subject)\s*(?::|(?:must|should)\s+(?:be|read)\b)',
    # Attention checks with the destination implied by an application section.
    r'\b(?:to\s+(?:show|prove|confirm|demonstrate)|so\s+(?:we|that\s+we)\s+know)\b'
    + CLAUSE + r'\b(?:read|reading|attention|human|bot)\b' + CLAUSE + ACTION,
    ACTION + CLAUSE + MARKER + CLAUSE + r'\b(?:read\s+(?:this|the|our)|attention\s+(?:check|to\s+detail)|not\s+a\s+bot)\b',
))


def find(description: str) -> list[str]:
    """Return evidence excerpts, not inferred phrases or generated cover letters.

    Whitespace is normalized for matching only. Keep neighboring text so "the
    phrase below" and its next-line literal stay visible to the reviewer.
    """
    text = re.sub(r'\s+', ' ', description or '').strip()
    spans = sorted((m.start(), m.end()) for pattern in PATTERNS for m in pattern.finditer(text))
    merged = []
    for start, end in spans:
        start, end = max(0, start - 100), min(len(text), end + 220)
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return [text[start:end] for start, end in merged]
