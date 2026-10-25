"""Platform bodies keep their old policies while moving behind injected adapters."""
import ast
import copy
import hashlib
from pathlib import Path
import unittest


class RestoreDependencies(ast.NodeTransformer):
    def __init__(self, linkedin):
        self.linkedin = linkedin

    def visit_Attribute(self, node):
        if isinstance(node.value, ast.Attribute) and isinstance(node.value.value, ast.Name) and node.value.value.id == 'self':
            if node.value.attr == 'services' or (self.linkedin and node.value.attr == 'core'):
                return ast.copy_location(ast.Name(id=node.attr, ctx=node.ctx), node)
        if isinstance(node.value, ast.Name) and node.value.id == 'self' and node.attr == 'core':
            return ast.parse('sys.modules[__name__]' if self.linkedin else 'core', mode='eval').body
        return self.generic_visit(node)


class PlatformIsolationTests(unittest.TestCase):
    def test_moved_platform_policies_match_pre_extraction_ast(self):
        # Frozen before extraction; whitespace/quote formatting and dependency
        # qualification are ignored, but changes to business logic are not.
        expected = {
            # Current-job-only Apply routing intentionally replaces the legacy
            # global button search; test_remoteok_details.py covers the policy.
            'remoteok': {'remoteok': 'b4424aed2d673957605fd7ae4ae647c3e4f1aa654e482f063a0d5ea578db9c05'},
            'himalayas': {'himalayas': '289702fe10a8e0aede4e63d4159b6524e1f640985acc19668262a953d7b36223'},
            'weworkremotely': {'weworkremotely': 'f1f75d6aa95f56b2d36cba13b639eca025b7cf31871308bafb96d37af7d9d6f1'},
            'wellfound': {'wellfound': 'd9e653e24ba2558399b0859520d464f04eee12b3081b92e38907a66675932156',
                          'wellfound_apply': 'd31dabfa783e5239f327b6bdc149a80715ed74a7440fce17d5911e71082c17f9'},
            'indeed': {'indeed': '4b20c878b59df82f633abec40d48338fd465d5d3ded59a06d5759f9079b649de',
                       'indeed_apply': 'e86d22e47b98767d3f17b7020189298f9d2b3124a9dfbb96ff0c08162248fae5',
                       # Intentionally updated for the bounded post-login search return;
                       # tests/test_indeed_search_resume.py covers its new policy.
                       'indeed_wait_ready': '19644631379d6f16f9cf5d2fece201b7cd45233a0e28a7b40f247752bf770cb2'},
            'linkedin': {'apply_to_job': 'f56d6b656804ced550e62064238da41bf3060b0a18cb912ef0f4ba948dce27e2',
                         'run_linkedin': '707100656c3b36e5784143c4e6e84fcaf399ecdf05d32c7fc85865a80c52270b',
                         'job_lists': '4bbd2d14dbb179e951ceee3596a3fe50b3ef55e87bd1560508e7083c789442a4'},
        }
        root = Path(__file__).resolve().parents[1]
        for platform, hashes in expected.items():
            tree = ast.parse((root / 'platforms' / (platform + '.py')).read_text(encoding='utf-8'))
            cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name.endswith('Adapter'))
            methods = {n.name: n for n in cls.body if isinstance(n, ast.FunctionDef)}
            for name, digest in hashes.items():
                with self.subTest(platform=platform, method=name):
                    restored = RestoreDependencies(platform == 'linkedin').visit(copy.deepcopy(methods[name]))
                    restored.args.args.pop(0)
                    actual = hashlib.sha256(ast.dump(restored, include_attributes=False).encode()).hexdigest()
                    self.assertEqual(actual, digest)

    def test_board_adapters_do_not_import_each_others_business_logic(self):
        root = Path(__file__).resolve().parents[1] / 'platforms'
        boards = {'linkedin', 'wellfound', 'indeed', 'remoteok', 'himalayas', 'weworkremotely'}
        for board in boards:
            tree = ast.parse((root / (board + '.py')).read_text(encoding='utf-8'))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    self.assertNotIn(node.module, boards - {board})
                    self.assertNotIn(node.module, {'platforms.' + other for other in boards - {board}})
