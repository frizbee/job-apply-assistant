"""Greenhouse application adapter; legacy/new hosts share a job identity."""
import re
from urllib.parse import parse_qs, urlparse
from .ats_base import ATSAdapter


class GreenhouseAdapter(ATSAdapter):
    name = 'greenhouse'

    @staticmethod
    def application_key(url):
        parsed = urlparse(url)
        match = re.fullmatch(r'/([\w.-]+)/jobs/([0-9]+)/?', parsed.path, flags=re.ASCII)
        if match:
            org, job = match[1], match[2]
        elif parsed.path.rstrip('/') == '/embed/job_app':
            params = parse_qs(parsed.query)
            if len(params.get('for', [])) != 1 or len(params.get('token', [])) != 1:
                return None
            org, job = params['for'][0], params['token'][0]
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', org) or not re.fullmatch(r'[0-9]+', job):
                return None
        else:
            return None
        region = 'eu' if '.eu.' in (parsed.hostname or '') else 'us'
        return f'greenhouse:{region}:{org.casefold()}:{job}'
