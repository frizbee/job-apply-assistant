"""Passive diagnostics retain error categories, not secret-bearing raw strings."""
import ast
from pathlib import Path
import re
import unittest
from urllib.parse import urlparse


SOURCE = Path(__file__).resolve().parents[1] / 'tools/indeed_challenge_diagnostic.py'
TREE = ast.parse(SOURCE.read_text(encoding='utf-8'))
NAMESPACE = {'re': re, 'urlparse': urlparse}
# Load pure helpers without importing a CLI that changes the process environment.
exec(compile(ast.Module(body=[node for node in TREE.body if isinstance(node, ast.FunctionDef)
                              and node.name in {'safe_url', 'error_summary'}], type_ignores=[]), str(SOURCE), 'exec'), NAMESPACE)


class DiagnosticPrivacyTests(unittest.TestCase):
    def test_urls_drop_credentials_query_and_opaque_paths(self):
        safe_url = NAMESPACE['safe_url']
        self.assertEqual(safe_url('https://username:PRIVATE@www.indeed.com/jobs?token=PRIVATE'), 'https://www.indeed.com/jobs')
        self.assertEqual(safe_url('https://challenges.cloudflare.com/a/PRIVATE?key=PRIVATE'), 'https://challenges.cloudflare.com/[challenge-resource]')
        self.assertEqual(safe_url('https://www.indeed.com/cdn-cgi/challenge-platform/PRIVATE'), 'https://www.indeed.com/[challenge-resource]')
        self.assertEqual(safe_url('https://accounts.indeed.com/auth/PRIVATE'), 'https://accounts.indeed.com/[resource]')

    def test_error_summary_has_no_arbitrary_console_payload(self):
        summary = NAMESPACE['error_summary']('Cloudflare challenge failed: 600010; token=PRIVATE; net::ERR_BLOCKED_BY_CLIENT')
        self.assertEqual(summary['challenge_error_codes'], ['600010'])
        self.assertEqual(summary['network_errors'], ['net::ERR_BLOCKED_BY_CLIENT'])
        self.assertIn('challenge', summary['categories'])
        self.assertNotIn('PRIVATE', str(summary))

    def test_diagnostic_does_not_intercept_requests_or_access_cookies(self):
        calls = [node.func.attr for node in ast.walk(TREE) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)]
        for forbidden in ['cookies', 'add_cookies', 'storage_state', 'route', 'reload', 'click', 'fill', 'add_init_script']:
            self.assertNotIn(forbidden, calls)


if __name__ == '__main__':
    unittest.main()
