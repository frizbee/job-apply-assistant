"""himalayas platform adapter; policy/sending guards are injected shared services."""
import datetime as dt
import html
import json
import re
import time
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus, urlparse
from playwright.sync_api import Page, Locator
from .base import PlatformAdapter

class HimalayasAdapter(PlatformAdapter):
    def __init__(self, services):
        super().__init__(services.core, services)

    def himalayas(self, page):
        seen = set()
        for kw in self.core.CFG['search']['keywords']:
            data = json.loads(self.services.http(f'https://himalayas.app/jobs/api/search?q={quote_plus(kw)}&limit=20'))
            for j in data.get('jobs', []):
                if j['guid'] in seen or not self.services.keyword_match(j['title']):
                    continue
                seen.add(j['guid'])
                job = {'id': 'himalayas:' + j['guid'].rsplit('/companies/', 1)[-1], 'title': j['title'], 'company': j.get('companyName', ''), 'url': j['applicationLink'], 'desc': self.services.strip_html(j.get('description', '')), 'location': ', '.join(j.get('locationRestrictions') or []), 'metadata': {'posted_at': j.get('pubDate'), 'workplace': self.services.job_metadata.workplace_kind(j.get('workplaceType'), j.get('remote')), 'location': ', '.join(j.get('locationRestrictions') or [])}}

                def apply(applied=None, job=job):
                    if not self.services.goto(page, job['url'], 'Himalayas'):
                        return 'blocked'
                    return self.services.external(page, job, self.services.click_apply(page), 'himalayas.app', applied)
                yield (job, apply)
