#!/usr/bin/env python3
"""Mechanical, one-time platform extraction. Dry-run by default; --write publishes.

Moves function bodies without policy changes, preserving legacy entry points for
tests/plugins. Generated adapters use explicit injected runtime/services, not a
copy of mutable global configuration. All generated modules are parsed before
publication; originals are archived outside the repository.
"""
import argparse
import ast
import copy
from pathlib import Path
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SOURCES = {
    'remoteok': ['remoteok'],
    'himalayas': ['himalayas'],
    'weworkremotely': ['wwr_login', 'weworkremotely'],
    'wellfound': ['wellfound_login', 'wellfound_apply', 'wellfound_search_verified', '_wellfound_public', 'wellfound'],
    'indeed': ['indeed_host', 'indeed_auth_pending', 'indeed_ready_evidence', 'indeed_wait_ready', 'indeed_apply', 'indeed'],
}
LINKEDIN = ['linkedin_submission_signals', 'wait_linkedin_submission', 'submit_linkedin', 'apply_to_job',
            'linkedin_navigation_issues', 'external_url', 'search_url', 'open_list', 'job_lists', 'job_info',
            'linkedin_login', 'run_linkedin']
CLASSES = {'remoteok': 'RemoteOKAdapter', 'himalayas': 'HimalayasAdapter', 'weworkremotely': 'WeWorkRemotelyAdapter',
           'wellfound': 'WellfoundAdapter', 'indeed': 'IndeedAdapter', 'linkedin': 'LinkedInAdapter', 'forms': 'FormEngine', 'ats': 'ATSEngine'}
ATS_FUNCTIONS = ['apply_form', 'apply_trusted_form', 'ats_apply_url', 'follow_apply', 'application_submission_signals',
                 'wait_application_submission', 'trusted_ats_document', 'check_ats_form', 'stable_application_controls',
                 'ensure_application_scope']
FORMS = ['question_of', 'click_form_control', 'application_button_action', 'click_navigation', 'radio_choice_confirmed',
         'fill_radios', 'fill_combobox', 'fill_yesno', 'unconfirmed_radio_questions', 'checkbox_choices',
         'checkbox_group_confirmed', 'unconfirmed_checkbox_questions', 'fill_checkgroups', 'standard_answer',
         'is_phone_country_control', 'combobox_value_confirmed', 'text_field_candidates', 'text_value_confirmed',
         'unconfirmed_text_questions', 'fill_step', '_fill_step', 'has_errors', 'native_form_issues']
FORM_CONSTANTS = ['QUESTION_JS', 'NON_SUBMITTING_CONTROL_JS', 'UNSAFE_SUBMIT_SELECTOR', 'NAVIGATION_BUTTON_RE',
                  'FINAL_BUTTON_RE', 'RADIO_GROUP_JS', 'RADIO_GROUPS', 'RADIO_SELECTION_JS', 'STANDARD_FIELDS',
                  'YESNO_JS', 'YESNO_SELECTION_JS', 'CHECKGROUP_STATE_JS', 'CHECKGROUP_JS', 'TEXT_FIELDS', 'FORM_VALUES_JS']
CONSTANTS = {'wellfound': ['WELLFOUND_OPENER_JS', 'WELLFOUND_SEARCH_JS'], 'indeed': ['INDEED_AUTH_PATH', 'INDEED_AUTH_HEADING']}
STDLIB = {'re', 'time', 'html', 'json', 'dt', 'urllib', 'ET', 'quote_plus', 'urlparse', 'parsedate_to_datetime', 'Page', 'Locator'}


class Dependencies(ast.NodeTransformer):
    def __init__(self, names, linkedin):
        self.names, self.linkedin = names, linkedin

    def visit_Subscript(self, node):
        if ast.dump(node, include_attributes=False) == ast.dump(ast.parse('sys.modules[__name__]', mode='eval').body, include_attributes=False):
            return ast.parse('self.core', mode='eval').body
        return self.generic_visit(node)

    def visit_Name(self, node):
        if node.id in self.names and node.id not in STDLIB and isinstance(node.ctx, ast.Load):
            target = 'self.core' if self.linkedin or node.id == 'core' else 'self.services'
            text = target if node.id == 'core' else target + '.' + node.id
            return ast.copy_location(ast.parse(text, mode='eval').body, node)
        return node


def exports(tree):
    names = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update(a.asname or a.name.split('.')[0] for a in node.names)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names.update(t.id for t in targets if isinstance(t, ast.Name))
    return names


def adapter(code, tree, platform, functions):
    linkedin = platform in ('linkedin', 'forms', 'ats')
    header = ('"""' + platform + ' platform adapter; policy/sending guards are injected shared services."""\n'
              'import datetime as dt\nimport html\nimport json\nimport re\nimport time\nimport urllib.request\n'
              'import xml.etree.ElementTree as ET\nfrom email.utils import parsedate_to_datetime\n'
              'from urllib.parse import quote_plus, urlparse\nfrom playwright.sync_api import Page, Locator\n'
              'from .base import PlatformAdapter\n\n')
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in CONSTANTS.get(platform, []) for t in node.targets):
            header += ast.get_source_segment(code, node) + '\n\n'
    methods = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in functions:
            method = Dependencies(exports(tree), linkedin).visit(copy.deepcopy(node))
            method.args.args.insert(0, ast.arg(arg='self'))
            ast.fix_missing_locations(method)
            methods.append(ast.unparse(method))
    constructor = '' if linkedin else '    def __init__(self, services):\n        super().__init__(services.core, services)\n\n'
    return header + f'class {CLASSES[platform]}(PlatformAdapter):\n' + constructor + '\n\n'.join(
        '\n'.join('    ' + line if line else '' for line in method.splitlines()) for method in methods) + '\n'


def facade_replacements(code, tree, mapping, linkedin):
    lines = code.splitlines(keepends=True)
    replacements = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in mapping:
            wrapper = copy.deepcopy(node)
            positional = [ast.Name(id=a.arg, ctx=ast.Load()) for a in node.args.posonlyargs + node.args.args]
            keywords = [ast.keyword(arg=a.arg, value=ast.Name(id=a.arg, ctx=ast.Load())) for a in node.args.kwonlyargs]
            factory = CLASSES[mapping[node.name]] + '(sys.modules[__name__])'
            call = ast.Call(func=ast.parse(factory + '.' + node.name, mode='eval').body, args=positional, keywords=keywords)
            wrapper.body = [ast.Return(value=call)]
            ast.fix_missing_locations(wrapper)
            replacements.append((node.lineno - 1, node.end_lineno, ast.unparse(wrapper) + '\n'))
        elif not linkedin and isinstance(node, ast.Assign):
            for platform, names in CONSTANTS.items():
                moved = [t.id for t in node.targets if isinstance(t, ast.Name) and t.id in names]
                if moved:
                    replacements.append((node.lineno - 1, node.end_lineno, f'from platforms.{platform} import ' + ', '.join(moved) + '\n'))
    for start, end, text in sorted(replacements, reverse=True):
        lines[start:end] = [text]
    imports = '\nimport sys\n' if not linkedin else '\n'
    for platform in dict.fromkeys(mapping.values()):
        imports += f'from platforms.{platform} import {CLASSES[platform]}\n'
    # Insert after the module docstring, preserving __future__ compatibility.
    insertion = tree.body[0].end_lineno if isinstance(tree.body[0], ast.Expr) else 0
    lines[insertion:insertion] = [imports]
    return ''.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--write', action='store_true')
    parser.add_argument('--forms', action='store_true', help='Extract the shared form engine after platform extraction')
    parser.add_argument('--ats', action='store_true', help='Extract shared ATS verification/dispatch after platform extraction')
    args = parser.parse_args()
    if args.forms or args.ats:
        kind = 'forms' if args.forms else 'ats'
        filename = 'form_engine.py' if args.forms else 'ats_engine.py'
        functions = FORMS if args.forms else ATS_FUNCTIONS
        constants = FORM_CONSTANTS if args.forms else []
        if (ROOT / filename).exists():
            print('Shared forms already extracted; refusing regeneration.')
            return
        path = ROOT / 'apply.py'
        code = path.read_text(encoding='utf-8')
        tree = ast.parse(code)
        CONSTANTS[kind] = constants
        generated_code = adapter(code, tree, kind, functions).replace('from .base import PlatformAdapter', 'from platforms.base import PlatformAdapter')
        facade = facade_replacements(code, tree, {name: kind for name in functions}, True).replace(f'from platforms.{kind} import {CLASSES[kind]}', f'from {filename[:-3]} import {CLASSES[kind]}')
        lines = facade.splitlines(keepends=True)
        for node in reversed(ast.parse(facade).body):
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                names = [t.id for t in targets if isinstance(t, ast.Name) and t.id in constants]
                if names:
                    lines[node.lineno - 1:node.end_lineno] = ['from form_engine import ' + ', '.join(names) + '\n']
        facade = ''.join(lines)
        ast.parse(generated_code)
        ast.parse(facade)
        print(filename, len(generated_code.splitlines()), 'lines; apply.py', len(facade.splitlines()), 'lines')
        if args.write:
            (ROOT / filename).write_text(generated_code, encoding='utf-8')
            path.write_text(facade, encoding='utf-8')
        return
    if (ROOT / 'platforms/linkedin.py').exists():
        print('Already extracted; refusing to regenerate edited adapters.')
        return
    generated = {ROOT / 'platforms/__init__.py': '"""Isolated job-board adapters with injected common safety services."""\n',
                 ROOT / 'platforms/base.py': '"""Runtime injection shared by otherwise independent platform adapters."""\n\n\nclass PlatformAdapter:\n    def __init__(self, core, services=None):\n        self.core = core\n        self.services = services if services is not None else core\n'}
    originals = {}
    for filename, groups in [('sources.py', SOURCES), ('apply.py', {'linkedin': LINKEDIN})]:
        path = ROOT / filename
        code = path.read_text(encoding='utf-8')
        originals[path] = code
        tree = ast.parse(code)
        for platform, functions in groups.items():
            generated[ROOT / 'platforms' / (platform + '.py')] = adapter(code, tree, platform, functions)
        mapping = {name: platform for platform, functions in groups.items() for name in functions}
        generated[path] = facade_replacements(code, tree, mapping, filename == 'apply.py')
    for path, code in generated.items():
        ast.parse(code, filename=str(path))
        print(path.relative_to(ROOT), len(code.splitlines()), 'lines')
    if args.write:
        temporary_root = (Path(tempfile.gettempdir()) / 'opencode').resolve()
        temporary_root.mkdir(exist_ok=True)
        backup = Path(tempfile.mkdtemp(prefix='platform-extraction-', dir=temporary_root))
        for path, code in originals.items():
            (backup / path.name).write_text(code, encoding='utf-8')
        for path, code in generated.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix('.extracting')
            temporary.write_text(code, encoding='utf-8')
            temporary.replace(path)
        print('Originals archived:', backup)


if __name__ == '__main__':
    main()
