"""Restore explicitly approved saved answers through the owning Playwright page.

This helper neither opens a job nor approves/advances/submits a review. The
caller must already own the selected application and the user's restore consent.
"""
import json
import re
from urllib.parse import urlparse


LABEL_JS = r'''e=>{
    const clean=s=>(s||'').replace(/\s+/g,' ').trim();
    return clean((e.getAttribute('aria-labelledby')||'').split(/\s+/).map(id=>document.getElementById(id)?.textContent||'').join(' '))||
        clean([...(e.labels||[])].map(l=>l.textContent).join(' '))||clean(e.getAttribute('aria-label'));
}'''
SELECTED_JS = r'''e=>[...(e.closest('.select__control')?.querySelectorAll('.select__single-value,.select__multi-value__label')||[])].map(n=>(n.textContent||'').replace(/\s+/g,' ').trim())'''


def restore_snapshot(page, form, snapshot, core, *, approved=False):
    if approved is not True:
        raise ValueError('Explicit permission to restore this application is required')
    expected, current = urlparse(snapshot['url']), urlparse(page.url)
    greenhouse = expected.hostname in ('job-boards.greenhouse.io', 'boards.greenhouse.io') and '/jobs/' in expected.path
    lever = expected.hostname == 'jobs.lever.co' and re.fullmatch(r'/[^/]+/[0-9a-fA-F-]{36}(?:/apply)?/?', expected.path)
    if (expected.scheme != 'https' or not (greenhouse or lever) or
            (current.scheme, current.netloc, current.path.rstrip('/')) !=
            (expected.scheme, expected.netloc, expected.path.rstrip('/'))):
        raise ValueError('Saved answers belong to a different application')
    controls = [c for c in snapshot['controls'] if c.get('tag') in ('INPUT', 'SELECT', 'TEXTAREA') and (c.get('id') or c.get('name')) and c.get('type') not in
                ('file', 'hidden', 'password', 'submit', 'button') and (c.get('role') != 'combobox' or c.get('selected'))]

    def field_for(control):
        if control.get('id'):
            selector = f'[id={json.dumps(control["id"])}]'
        else:
            selector = f'[name={json.dumps(control["name"])}]'
            if control['type'] == 'radio':
                selector += f'[value={json.dumps(control["value"])}]'
        field = form.locator(selector)
        if (field.count() != 1 or field.evaluate('e=>e.type') != control['type']
                or field.evaluate(LABEL_JS) != control['label']):
            raise ValueError('The form question changed: ' + (control.get('id') or control['name']))
        if control.get('question'):
            question = field.evaluate(r'''e=>(e.closest('.application-question')?.querySelector('.application-label')?.textContent||'').replace(/\s+/g,' ').trim()''')
            if question and question != control['question']:
                raise ValueError('The form question changed: ' + (control.get('id') or control['name']))
        return field

    def key(control):
        return control.get('id') or (control['name'] + (':' + control['value'] if control['type'] == 'radio' else ''))

    for control in controls:
        if control['id'] == 'race' and not form.locator('[id="race"]').count():
            continue  # Conditional Greenhouse EEO widget, revealed after ethnicity.
        field_for(control)
    restored, failed = [], []
    for control in controls:
        if core.STOP.is_set():
            raise core.Stopped
        try:
            field = field_for(control)
            if lever and control.get('id') == 'location-input' and snapshot.get('leverLocation'):
                target = snapshot['leverLocation']
                selected = form.locator('#selected-location')
                def location_matches():
                    try:
                        actual = json.loads(selected.input_value())
                        # Lever may issue a new opaque ID for the same observed
                        # canonical location on reopening. Never copy the old ID.
                        normalize = lambda text: ' '.join((text or '').split()).casefold()
                        return (bool(actual.get('id')) and normalize(actual.get('name')) == normalize(target.get('name'))
                                and normalize(field.input_value()) == normalize(control.get('value') or target.get('name')))
                    except (ValueError, AttributeError):
                        return False
                if not location_matches():
                    field.fill('')
                    field.press_sequentially(control.get('value') or target['name'], delay=20)
                    for _ in range(28):
                        page.wait_for_timeout(250)
                        options = field.locator('xpath=ancestor::*[contains(@class,"application-question")][1]').locator('.dropdown-results > *:visible')
                        labels = options.all_inner_texts()
                        matches = [i for i, text in enumerate(labels) if ' '.join(text.split()) == target['name']]
                        if len(matches) == 1:
                            core.click_form_control(options.nth(matches[0]))
                            break
                (restored if location_matches() else failed).append(key(control))
                continue
            if control.get('role') != 'combobox':
                if control['type'] in ('checkbox', 'radio'):
                    if control['type'] == 'radio' and not control['checked']:
                        continue  # Restore the positively selected member, then verify the whole group below.
                    if field.is_checked() != control['checked']:
                        labels = form.locator(f'label[for={json.dumps(control["id"])}]') if control.get('id') else field.locator('xpath=ancestor::label[1]')
                        core.click_form_control(labels if labels.count() == 1 else field)
                    valid = field.is_checked() == control['checked']
                elif control['tag'] == 'SELECT':
                    field.select_option(value=control.get('value') or '')
                    valid = field.input_value() == (control.get('value') or '')
                else:
                    field.fill(control.get('value') or '')
                    valid = field.input_value() == (control.get('value') or '')
                (restored if valid else failed).append(key(control))
                continue
            wanted = control['selected']
            if field.evaluate(SELECTED_JS) == wanted:
                restored.append(key(control))
                continue
            if len(wanted) != 1:
                failed.append(key(control))
                continue
            # A multi-select may start with a site/app default (e.g. a privacy
            # opt-out). Replaying a saved answer must replace, not append to,
            # that default. Remove only chips absent from the approved snapshot.
            control_root = field.locator('xpath=ancestor::*[contains(concat(" ",normalize-space(@class)," ")," select__control ")][1]')
            for _ in range(len(field.evaluate(SELECTED_JS)) + 1):
                extra = None
                for chip in control_root.locator('.select__multi-value').all():
                    label = chip.locator('.select__multi-value__label')
                    if label.count() == 1 and ' '.join(label.inner_text().split()) not in wanted:
                        extra = chip
                        break
                if extra is None:
                    break
                remove = extra.locator('.select__multi-value__remove')
                if remove.count() != 1:
                    raise ValueError('Cannot safely remove an extra selection')
                before = field.evaluate(SELECTED_JS)
                core.click_form_control(remove)
                page.wait_for_timeout(250)
                if field.evaluate(SELECTED_JS) == before:
                    raise ValueError('Extra selection was not removed')
            if field.evaluate(SELECTED_JS) == wanted:
                restored.append(key(control))
                continue
            queries = [wanted[0]]
            if control['id'] == 'candidate-location' and ',' in wanted[0]:
                queries.append(wanted[0].split(',')[0])
            success = False
            for query in queries:
                core.click_form_control(field)
                field.fill(query)
                for _ in range(28):
                    if core.STOP.is_set():
                        raise core.Stopped
                    page.wait_for_timeout(250)
                    owner = field.get_attribute('aria-controls') or field.get_attribute('aria-owns') or ('react-select-' + control['id'] + '-listbox')
                    options = page.locator(f'[id={json.dumps(owner)}] [role="option"]:visible')
                    labels = options.all_inner_texts()
                    matches = [i for i, label in enumerate(labels) if ' '.join(label.split()).casefold() == wanted[0].casefold()]
                    if control['id'] == 'country' and wanted[0].startswith('+'):
                        index = core.application_answers.country_code_option(wanted, labels)
                        matches = [] if index is None else [index]
                    if len(matches) != 1:
                        continue
                    core.click_form_control(options.nth(matches[0]))
                    page.wait_for_timeout(250)
                    success = field.evaluate(SELECTED_JS) == wanted
                    if success:
                        break
                if success:
                    break
                field.press('Escape')
            (restored if success else failed).append(key(control))
        except Exception as error:
            if core.browser_closed(error):
                raise core.BrowserClosed from None
            failed.append(key(control))
    # Re-read all answers after conditional dropdowns have rerendered the form.
    for control in controls:
        if key(control) in failed:
            continue
        field = field_for(control)
        if control.get('role') == 'combobox':
            same = field.evaluate(SELECTED_JS) == control['selected']
        elif control['type'] in ('checkbox', 'radio'):
            same = field.is_checked() == control['checked']
        else:
            same = field.input_value() == (control.get('value') or '')
        if not same:
            failed.append(key(control))
    return {'restored': restored, 'not_restored': list(dict.fromkeys(failed)), 'continued': False, 'submitted': False}
