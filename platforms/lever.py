"""Lever application adapter and exact provider job identity."""
import re
from urllib.parse import urlparse
from uuid import UUID
from .ats_base import ATSAdapter


class LeverAdapter(ATSAdapter):
    name, suffix = 'lever', '/apply'

    @staticmethod
    def application_key(url):
        parsed = urlparse(url)
        match = re.fullmatch(r'/([\w.-]+)/([0-9a-fA-F-]{36})(?:/apply)?/?', parsed.path, flags=re.ASCII)
        if not match:
            return None
        try:
            job = UUID(match[2])
        except ValueError:
            return None
        region = 'eu' if parsed.hostname == 'jobs.eu.lever.co' else 'us'
        return f'lever:{region}:{match[1].casefold()}:{job}'
