"""Choose an existing diagnostic tab; never close or overwrite unrelated tabs."""
from urllib.parse import urlparse


def is_indeed_url(url, domain='www.indeed.com'):
    host = (urlparse(url).hostname or '').lower()
    configured = domain.lower().removeprefix('www.')
    return bool(host) and (host == 'indeed.com' or host.endswith('.indeed.com')
                           or host == configured or host.endswith('.' + configured))


def diagnostic_url(url):
    """Navigation evidence without credentials, OAuth codes/state or query tokens."""
    parsed = urlparse(url)
    if not parsed.hostname:
        return '[local-document]'
    allowed = {'/', '/jobs', '/jobs/', '/account/login', '/auth', '/bff/oauth/login',
               '/oauth/v2/authorize', '/bff/oauth/callback'}
    path = parsed.path if parsed.path in allowed else '/[resource]'
    return f'{parsed.scheme}://{parsed.hostname}{path}'


def reuse_page(context, domain='www.indeed.com'):
    pages = [page for page in context.pages if not page.is_closed()]
    # Preserve an existing Indeed page (including a login in progress) instead
    # of making a second tab beside it. Do not inspect credentials or cookies.
    matching = [page for page in pages if is_indeed_url(page.url, domain)]
    if matching:
        return matching[-1], True
    blank = [page for page in pages if page.url in ('about:blank', '')]
    if blank:
        return blank[0], True
    return context.new_page(), False
