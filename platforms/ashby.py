"""Ashby application adapter and exact job identity."""
import re
from urllib.parse import urlparse
from uuid import UUID
from .ats_base import ATSAdapter


class AshbyAdapter(ATSAdapter):
    name, suffix = 'ashby', '/application'

    @staticmethod
    def application_key(url):
        match = re.fullmatch(r'/([\w.-]+)/([0-9a-fA-F-]{36})(?:/application)?/?', urlparse(url).path, flags=re.ASCII)
        if not match:
            return None
        try:
            return f'ashby:{match[1].casefold()}:{UUID(match[2])}'
        except ValueError:
            return None
