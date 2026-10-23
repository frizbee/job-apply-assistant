"""Shared verified ATS form flow; URL identities remain provider-specific."""
from urllib.parse import urlparse
from .base import PlatformAdapter


class ATSAdapter(PlatformAdapter):
    name = ''
    suffix = ''

    @classmethod
    def application_url(cls, url):
        parsed = urlparse(url)
        path = parsed.path.rstrip('/')
        if cls.suffix and not path.endswith(cls.suffix):
            path += cls.suffix
        return parsed._replace(path=path or '/').geturl()

    def apply(self, page, job_id, title, company, url, history):
        return self.core.apply_trusted_form(page, job_id, title, company, url, self.name, history)
