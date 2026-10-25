"""Diagnostics reuse the profile's tab instead of opening another login tab."""
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock
from tools.indeed_browser_session import reuse_page, is_indeed_url, diagnostic_url


def tab(url, closed=False):
    return SimpleNamespace(url=url, is_closed=lambda: closed)


class SessionTabTests(unittest.TestCase):
    def test_existing_login_is_not_replaced_with_new_tab(self):
        login = tab('https://secure.indeed.com/auth?continue=private')
        context = MagicMock(pages=[tab('about:blank'), login])
        self.assertEqual(reuse_page(context), (login, True))
        context.new_page.assert_not_called()

    def test_blank_startup_tab_is_reused(self):
        blank = tab('about:blank')
        context = MagicMock(pages=[blank])
        self.assertEqual(reuse_page(context), (blank, True))
        context.new_page.assert_not_called()

    def test_unrelated_tabs_are_not_overwritten(self):
        context = MagicMock(pages=[tab('https://www.linkedin.com/jobs/')])
        page, reused = reuse_page(context)
        self.assertIs(page, context.new_page.return_value)
        self.assertFalse(reused)

    def test_closed_tab_is_not_reused(self):
        blank = tab('about:blank')
        context = MagicMock(pages=[tab('https://www.indeed.com/', closed=True), blank])
        self.assertEqual(reuse_page(context), (blank, True))

    def test_host_matching_does_not_accept_lookalikes(self):
        self.assertTrue(is_indeed_url('https://ar.indeed.com/'))
        self.assertFalse(is_indeed_url('https://notindeed.com/'))
        self.assertFalse(is_indeed_url('https://indeed.com.evil.test/'))

    def test_navigation_logs_never_contain_oauth_secrets(self):
        self.assertEqual(diagnostic_url('https://authentication-session-manager.indeed.com/bff/oauth/callback?code=PRIVATE&state=PRIVATE'),
                         'https://authentication-session-manager.indeed.com/bff/oauth/callback')
        self.assertNotIn('PRIVATE', diagnostic_url('https://user:PRIVATE@secure.indeed.com/auth/PRIVATE?token=PRIVATE'))


if __name__ == '__main__':
    unittest.main()
