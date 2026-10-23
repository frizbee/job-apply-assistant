"""remoteok platform adapter; policy/sending guards are injected shared services."""
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

class RemoteOKAdapter(PlatformAdapter):
    def __init__(self, services):
        super().__init__(services.core, services)

    def remoteok(self, page):
        feeds = [''] + [f'?tag={self.services.slug(kw)}' for kw in self.core.CFG['search']['keywords']] + ['?tag=ai', '?tag=machine-learning']
        jobs = {}
        for q in dict.fromkeys(feeds):
            for j in json.loads(self.services.http(f'https://remoteok.com/api{q}'))[1:]:
                jobs[j['id']] = j
            time.sleep(1)
        for j in jobs.values():
            if not self.services.keyword_match(j.get('position', '')):
                continue
            job = {'id': f'remoteok:{j['id']}', 'title': j['position'], 'company': j.get('company', ''), 'url': j['url'], 'desc': self.services.strip_html(j.get('description', '')), 'location': j.get('location', ''), 'metadata': {'posted_at': j.get('epoch') or j.get('date'), 'workplace': self.services.job_metadata.workplace_kind(j.get('workplaceType'), j.get('remote'), j.get('location')), 'location': j.get('location', '')}}

            def apply(applied=None, job=job):
                if not self.services.goto(page, job['url'], 'Remote OK'):
                    return 'blocked'
                return self.services.external(page, job, self.services.click_apply(page), 'remoteok.com', applied)
            yield (job, apply)
