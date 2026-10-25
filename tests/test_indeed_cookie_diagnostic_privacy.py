"""Cookie rejection diagnostics must never persist cookie values/Set-Cookie."""
import ast
from pathlib import Path
import re
import unittest


SOURCE = Path(__file__).resolve().parents[1] / 'tools/indeed_cookie_diagnostic.py'
TREE = ast.parse(SOURCE.read_text(encoding='utf-8'))
HELPERS = {'re': re}
exec(compile(ast.Module(body=[node for node in TREE.body if isinstance(node, ast.FunctionDef)
                              and node.name in {'cookie_summary', 'policy_summary'}], type_ignores=[]), str(SOURCE), 'exec'), HELPERS)


class CookieDiagnosticPrivacyTests(unittest.TestCase):
    def test_request_and_response_cookie_values_are_dropped(self):
        summary = HELPERS['cookie_summary']([
            {'cookie': {'name': 'cf_clearance', 'value': 'PRIVATE', 'domain': 'private.example'}, 'blockedReasons': []},
            {'cookie': {'name': '__cf_bm', 'value': 'PRIVATE'}, 'blockedReasons': ['ThirdPartyPhaseout']},
            {'cookieLine': 'session=PRIVATE', 'blockedReasons': ['SecureOnly']},
        ])
        self.assertNotIn('PRIVATE', str(summary))
        self.assertNotIn('private.example', str(summary))
        self.assertEqual(summary[0], {'category': 'clearance', 'blocked_reasons': []})
        self.assertEqual(summary[1]['blocked_reasons'], ['ThirdPartyPhaseout'])

    def test_policy_nonces_and_arbitrary_text_are_dropped(self):
        summary = HELPERS['policy_summary']("[Report Only] Refused script-src 'nonce-PRIVATE' unsafe-eval")
        self.assertTrue(summary['report_only'])
        self.assertTrue(summary['unsafe_eval_mentioned'])
        self.assertIn('script-src', summary['directives'])
        self.assertNotIn('PRIVATE', str(summary))

    def test_diagnostic_does_not_change_browser_or_extract_cookie_store(self):
        methods = [node.func.attr for node in ast.walk(TREE) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)]
        for name in ('cookies', 'add_cookies', 'storage_state', 'route', 'reload', 'click', 'fill', 'add_init_script'):
            self.assertNotIn(name, methods)


if __name__ == '__main__':
    unittest.main()
