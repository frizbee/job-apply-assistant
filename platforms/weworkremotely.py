"""weworkremotely platform adapter; policy/sending guards are injected shared services."""
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

class WeWorkRemotelyAdapter(PlatformAdapter):
    def __init__(self, services):
        super().__init__(services.core, services)

    def wwr_login(self, page) -> bool:
        if 'wwr' not in self.services.LOGGED_IN:
            page.goto('https://weworkremotely.com/job-seekers/account/login', wait_until='domcontentloaded')
            self.services.LOGGED_IN['wwr'] = self.core.wait_human(page, lambda p: not re.search('account/(login|register|sign)', p.url) and self.services.page_ready(p), 'sign in to We Work Remotely (a free account is required to view application links)')
        return self.services.LOGGED_IN['wwr']

    def weworkremotely(self, page):
        root = ET.fromstring(self.services.http('https://weworkremotely.com/remote-jobs.rss'))
        for item in root.iter('item'):
            company, _, title = (item.findtext('title') or '').partition(': ')
            try:
                posted = parsedate_to_datetime(item.findtext('pubDate'))
            except (TypeError, ValueError):
                posted = None
            if not title or not self.services.keyword_match(title):
                continue
            link = item.findtext('link') or ''
            job = {'id': 'wwr:' + link.rstrip('/').rsplit('/', 1)[-1], 'title': title, 'company': company, 'url': link, 'desc': self.services.strip_html(item.findtext('description')), 'location': item.findtext('region') or '', 'metadata': {'posted_at': posted, 'workplace': self.services.job_metadata.workplace_kind(item.findtext('workplaceType'), item.findtext('region')), 'location': item.findtext('region') or ''}}

            def apply(applied=None, job=job):
                if not self.services.goto(page, job['url'], 'We Work Remotely'):
                    return 'blocked'
                if page.locator("a[href*='account/register']").count():
                    if not self.services.wwr_login(page) or not self.services.goto(page, job['url'], 'We Work Remotely'):
                        return 'blocked'
                return self.services.external(page, job, self.services.click_apply(page), 'weworkremotely.com', applied)
            yield (job, apply)
