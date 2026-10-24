"""forms platform adapter; policy/sending guards are injected shared services."""
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
from platforms.base import PlatformAdapter

QUESTION_JS = r"""el => {
  const t = s => (s || '').replace(/\s+/g, ' ').trim();
  if (el.id) {
    const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
    if (l && t(l.innerText) && el.type !== 'radio') return t(l.innerText);
  }
  const lb = el.getAttribute('aria-labelledby');
  if (lb) {
    const txt = lb.split(' ').map(id => document.getElementById(id)).filter(Boolean).map(e => t(e.innerText)).join(' ');
    if (txt) return txt;
  }
  const fs = el.closest('fieldset');
  if (fs) { const lg = fs.querySelector('legend'); if (lg && t(lg.innerText)) return t(lg.innerText); }
  // New LinkedIn form: climb to the first meaningful line if the question is unbound.
  const noise = /^(\d+\s*\/\s*\d+.*|.*karakter|.*characters?|geçersiz giriş|invalid.*|.*zorunlu.*|.*required.*|yes|no|evet|hayır)$/i;
  const group = el.type === 'radio' && el.name
    ? [...document.querySelectorAll(`input[type=radio][name="${CSS.escape(el.name)}"]`)] : [el];
  const opts = new Set(group.map(r => t((r.id && document.querySelector(`label[for="${CSS.escape(r.id)}"]`) || r.parentElement || {}).innerText)));
  let p = el.parentElement;
  for (let i = 0; i < 8 && p; i++, p = p.parentElement) {
    if (!group.every(r => p.contains(r))) continue;
    const lines = (p.innerText || '').split('\n').map(t).filter(l => l && !noise.test(l) && !opts.has(l));
    if (lines.length) return lines[0].slice(0, 300);
  }
  return t(el.getAttribute('aria-label') || el.getAttribute('placeholder'));
}"""

UNSAFE_SUBMIT_SELECTOR = "button:not([type='button' i]), button[formaction], input[type='submit' i], input[type='image' i]"

NON_SUBMITTING_CONTROL_JS = r"""el => {
    const submits = e => e && ((e.tagName === 'BUTTON' && (e.type !== 'button' || e.hasAttribute('formaction')))
        || (e.tagName === 'INPUT' && /^(submit|image)$/i.test(e.type)));
    if (submits(el.closest('button, input'))) return false;
    if (submits(el.parentElement?.closest('button, input'))) return false;
    const label = el.closest('label');
    if (label && submits(label.control)) return false;
    return !el.querySelector("button:not([type='button' i]), button[formaction], input[type='submit' i], input[type='image' i]");
}"""

NAVIGATION_BUTTON_RE = re.compile(
    r"^\s*(?:next(?:\s+step)?|continue(?:\s+to\s+(?:the\s+)?(?:next\s+step|review(?:\s+your\s+application)?))?|"
    r"review(?:\s+(?:your\s+)?application)?|devam(?:\s+et)?|[iİıI]leri|(?:başvuruyu\s+)?(?:[iİıI]ncele|gözden\s+geç[iİıI]r))\s*$", re.I)

FINAL_BUTTON_RE = re.compile(
    r"^\s*(?:submit(?:\s+(?:your\s+)?application)?|send\s+(?:your\s+)?application|"
    r"(?:başvur(?:u|unu|uyu|unuzu)\s+)?gönder)\s*$", re.I)

RADIO_GROUP_JS = r"""fs => {
  const t = s => (s || '').replace(/\s+/g, ' ').trim();
  const lines = el => (el.innerText || '').split('\n').map(t).filter(Boolean);
  const roles = [...fs.querySelectorAll('[role=radio]')];
  const labels = roles.length
    ? roles.map(r => lines(r).pop() || '')
    : [...fs.querySelectorAll('input[type=radio]')].map(r => {
        const l = r.id && document.querySelector(`label[for="${CSS.escape(r.id)}"]`);
        if (l && t(l.innerText)) return t(l.innerText);
        // If the label is empty, use the nearest ancestor with option text.
        let p = r.parentElement;
        while (p && p !== fs && !t(p.innerText)) p = p.parentElement;
        return p && p !== fs ? t(p.innerText) : '';
      });
  let q = '';
  const lg = fs.querySelector('legend');
  if (lg) q = t(lg.innerText);
  if (!q && fs.parentElement) {
    const own = new Set(lines(fs));
    q = lines(fs.parentElement).find(l => !own.has(l)) || '';
  }
  if (!q && roles.length) q = t(roles[0].getAttribute('aria-label'));
  return [q, labels];
}"""

RADIO_GROUPS = "fieldset:has(input[type='radio']):visible, [role='radiogroup']:visible"

RADIO_SELECTION_JS = r"""root => {
    const native = [...root.querySelectorAll('input[type=radio]')];
    const roles = [...root.querySelectorAll('[role=radio]')];
    const nativeState = input => {
        const aria = input.getAttribute('aria-checked');
        return aria === null || aria === String(input.checked) ? input.checked : null;
    };
    if (!roles.length) return native.map(nativeState);
    // Do not overlook an actual submitted radio value outside the custom options.
    if (native.some(input => !roles.some(option => option === input || option.contains(input)))) return [];
    return roles.map(option => {
        if (option.matches('input[type=radio]')) return nativeState(option);
        const aria = option.getAttribute('aria-checked');
        if (aria !== 'true' && aria !== 'false') return null;
        const selected = aria === 'true';
        const backing = [...option.querySelectorAll('input[type=radio]')];
        if (backing.length > 1 || (backing.length === 1 && nativeState(backing[0]) !== selected)) return null;
        return selected;
    });
}"""

STANDARD_FIELDS = [
    (r"first.?name|given.?name|vorname|\bad[ıi]?\b(?!.*soyad)", "first_name"),
    (r"last.?name|surname|family.?name|soyad|nachname", "last_name"),
    (r"full.?name|^name\b|your name|ad soyad", "full_name"),
    (r"e-?mail", "email"),
    (r"phone|mobile|telefon|\btel\b", "phone_intl"),
    (r"linkedin", "linkedin"),
    (r"github", "github"),
    (r"website|portfolio|personal (site|url)|other url", "github"),
    (r"current (company|employer)|^org\b| org$", "current_company"),
    (r"current (title|position|role)", "current_title"),
    (r"\blocation|\bcity\b|şehir|where are you (based|located)", "city"),
]

YESNO_JS = r"""root => {
  // Mark Ashby Yes/No button pairs and return [question, any semantic selection].
  const t = s => (s || '').replace(/\s+/g, ' ').trim();
  const out = [];
  const yes = [...root.querySelectorAll('button')].filter(b => /^(yes|evet)$/i.test(t(b.innerText)));
  for (const b of yes) {
    const box = b.parentElement;
    const btns = [...box.querySelectorAll('button')];
    if (btns.length !== 2 || !/^(no|hayır)$/i.test(t(btns[1].innerText))) continue;
    let p = box.parentElement, q = '';
    for (let i = 0; i < 5 && p && !q; i++, p = p.parentElement)
      q = (p.innerText || '').split('\n').map(t).find(l => l && !/^(yes|no|evet|hayır)$/i.test(l)) || '';
    const picked = btns.some(x => ['aria-pressed', 'aria-checked', 'aria-selected'].some(a => x.getAttribute(a) === 'true'));
    box.setAttribute('data-yesno', out.length);
    out.push([q, picked]);
  }
  return out;
}"""

YESNO_SELECTION_JS = r"""box => {
    const t = s => (s || '').replace(/\s+/g, ' ').trim();
    const buttons = [...box.querySelectorAll('button')];
    if (buttons.length !== 2 || !/^(yes|evet)$/i.test(t(buttons[0].innerText))
        || !/^(no|hayır)$/i.test(t(buttons[1].innerText))) return {question: '', states: []};
    let p = box.parentElement, question = '';
    for (let i = 0; i < 5 && p && !question; i++, p = p.parentElement)
        question = (p.innerText || '').split('\n').map(t).find(l => l && !/^(yes|no|evet|hayır)$/i.test(l)) || '';
    const states = buttons.map(button => {
        const signals = ['aria-pressed', 'aria-checked', 'aria-selected'].map(a => button.getAttribute(a)).filter(v => v !== null);
        if (!signals.length || signals.some(v => v !== 'true' && v !== 'false') || signals.some(v => v !== signals[0])) return null;
        const selected = signals[0] === 'true';
        const backing = [...button.querySelectorAll('input[type=radio], input[type=checkbox]')];
        if (backing.length > 1 || (backing.length === 1 && backing[0].checked !== selected)) return null;
        return selected;
    });
    return {question, states};
}"""

CHECKGROUP_STATE_JS = r"""g => {
  const t = s => (s || '').replace(/\s+/g, ' ').trim();
  const label = c => {
    const l = c.id && document.querySelector(`label[for="${CSS.escape(c.id)}"]`);
    if (l && t(l.innerText)) return t(l.innerText);
    let p = c.parentElement;
    while (p && !t(p.innerText)) p = p.parentElement;
    return p ? t(p.innerText) : '';
  };
  const cbs = [...g.querySelectorAll('input[type=checkbox]')];
  const opts = cbs.map(label);
  let p = g, q = '';
  for (let i = 0; i < 4 && p && !q; i++, p = p.parentElement)
    q = (p.innerText || '').split('\n').map(t).find(l => l && !opts.includes(l)) || '';
  return {question: q, options: opts,
    states: cbs.map(c => {
      const aria = c.getAttribute('aria-checked');
      return c.indeterminate || (aria !== null && aria !== String(c.checked)) ? null : c.checked;
    }),
    disabled: cbs.map(c => c.matches(':disabled') || c.getAttribute('aria-disabled') === 'true')};
}"""

CHECKGROUP_JS = r"""root => {
  // Refresh markers: removed/restructured groups must not leave stale exemptions.
  for (const g of root.querySelectorAll('[data-checkgroup]')) g.removeAttribute('data-checkgroup');
  const groups = new Set();
  for (const c of root.querySelectorAll('input[type=checkbox]')) {
    let g = c.parentElement;
    while (g && g !== root && g.querySelectorAll('input[type=checkbox]').length < 2) g = g.parentElement;
    if (!g || g === root) continue;
    groups.add(g);
  }
  const out = [];
  const inspect = (""" + CHECKGROUP_STATE_JS + r""");
  for (const g of groups) {
    const state = inspect(g);
    g.setAttribute('data-checkgroup', out.length);
    out.push([state.question, state.options, state.states.some(s => s === true)]);
  }
  return out;
}"""

TEXT_FIELDS = (
    "input[type='text']:visible:not([aria-hidden='true']), input[type='tel']:visible:not([aria-hidden='true']), input[type='email']:visible:not([aria-hidden='true']), "
    "input[type='number']:visible:not([aria-hidden='true']), input[type='url']:visible:not([aria-hidden='true']), input[type='date']:visible:not([aria-hidden='true']), "
    "input:not([type]):visible:not([aria-hidden='true']), textarea:visible:not([aria-hidden='true'])"
)

FORM_VALUES_JS = """root => [...root.querySelectorAll('input,select,textarea')].map(e =>
    [e.tagName, e.type, e.name, e.value, e.checked, e.files ? [...e.files].map(f=>f.name) : null])"""

class FormEngine(PlatformAdapter):
    def question_of(self, el: Locator) -> str:
        try:
            return el.evaluate(self.core.QUESTION_JS) or ''
        except Exception:
            return ''

    def click_form_control(self, control: Locator, timeout: int=5000) -> None:
        """Reject native submit actions during filling, in both test and live modes."""
        if control.evaluate(self.core.NON_SUBMITTING_CONTROL_JS) is not True:
            raise self.core.UnsafeFormControl('submit-capable control requires manual review')
        safe_selector = f':not(:is({self.core.UNSAFE_SUBMIT_SELECTOR})):not(:is({self.core.UNSAFE_SUBMIT_SELECTOR}) *):not(:has(:is({self.core.UNSAFE_SUBMIT_SELECTOR})))'
        control.and_(control.page.locator(safe_selector)).click(timeout=timeout)

    def application_button_action(self, button: Locator) -> str:
        """Unknown or conflicting text/ARIA labels must never default to Next."""
        labels = [label.strip() for label in (button.inner_text(), button.get_attribute('aria-label') or '') if label.strip()]
        actions = {'submit' if self.core.FINAL_BUTTON_RE.fullmatch(label) else 'next' if self.core.NAVIGATION_BUTTON_RE.fullmatch(label) else 'unknown' for label in labels}
        return actions.pop() if len(actions) == 1 else 'unknown'

    def click_navigation(self, button: Locator) -> None:
        if self.core.application_button_action(button) != 'next':
            raise self.core.UnsafeFormControl('unrecognized navigation action')
        navigation = button.and_(button.page.get_by_role('button', name=self.core.NAVIGATION_BUTTON_RE))
        if button.inner_text().strip():
            navigation = navigation.filter(has_text=self.core.NAVIGATION_BUTTON_RE)
        self.core.click_form_control(navigation)

    def radio_choice_confirmed(self, states, index) -> bool:
        """Unknown/inconsistent state is not False: require one exact, exclusive choice."""
        return isinstance(states, list) and type(index) is int and (0 <= index < len(states)) and (states[index] is True) and all((state is False for i, state in enumerate(states) if i != index))

    def fill_radios(self, scope: Locator) -> list[str]:
        """Fill radio questions and return unresolved ones. New LinkedIn forms use
        div[role=radio] with empty labels; older forms use legend and label[for]."""
        unanswered: list[str] = []
        for fs in scope.locator(self.core.RADIO_GROUPS).all():
            if self.core.linkedin_forms.resume_group(fs):
                if not self.core.linkedin_forms.verified_group(fs, self.core):
                    unanswered.append('Resume selection is not confirmed')
                continue
            q = ''
            try:
                q, labels = fs.evaluate(self.core.RADIO_GROUP_JS)
                idx = self.core.choose_option(q, labels) if q.strip() else None
                if idx is None:
                    self.core.log(f'    ? unanswered question: {q[:90]}')
                    unanswered.append(q or 'radio group')
                    continue
                before = fs.evaluate(self.core.RADIO_SELECTION_JS)
                if len(before) != len(labels):
                    unanswered.append(q)
                    continue
                if not self.core.radio_choice_confirmed(before, idx):
                    items = fs.locator("[role='radio']")
                    if items.count():
                        target = items.nth(idx)
                        if not target.is_enabled():
                            unanswered.append(q)
                            continue
                        self.core.click_form_control(target)
                    else:
                        radio = fs.locator("input[type='radio']").nth(idx)
                        if radio.is_disabled():
                            unanswered.append(q)
                            continue
                        try:
                            radio.check(timeout=2000)
                        except self.core.PWTimeout:
                            if not radio.is_checked():
                                row = radio.locator("xpath=ancestor::*[normalize-space(.)!=''][1]")
                                self.core.click_form_control(row)
                                if not radio.is_checked():
                                    radio.check(force=True, timeout=2000)
                self.core.pause(0.3, 0.7)
                current_q, current_labels = fs.evaluate(self.core.RADIO_GROUP_JS)
                after = fs.evaluate(self.core.RADIO_SELECTION_JS)
                if current_q != q or current_labels != labels or len(after) != len(labels) or (not self.core.radio_choice_confirmed(after, idx)):
                    self.core.log(f'    ? radio selection could not be verified: {q[:90]}')
                    unanswered.append(q)
            except Exception as e:
                if self.core.browser_closed(e):
                    raise self.core.BrowserClosed() from None
                self.core.log(f'    ! could not fill radio group: {e.__class__.__name__}')
                unanswered.append(q or self.core.question_of(fs) or 'radio group')
        return unanswered

    def fill_combobox(self, page: Page, el: Locator, q: str) -> bool:
        """Fill searchable Greenhouse/Ashby dropdowns (role=combobox)."""
        std = self.core.standard_answer(el, q)
        cands = [std] if std else self.core.answer(q)
        if not cands:
            return False
        try:
            self.core.click_form_control(el)
            self.core.pause(0.5, 1)
            phone_country = self.core.is_phone_country_control(el, q)
            if std and (not phone_country) and (self.core.application_answers.contact_kind(q) != 'country_code'):
                el.fill(std.split(',')[0])
                self.core.pause(1.5, 2.5)
            opts = page.locator("[role='option']:visible")
            texts = opts.all_inner_texts()
            if not texts:
                el.press('Escape')
                return False
            if phone_country:
                idx = self.core.application_answers.country_code_option(cands, texts)
            else:
                idx = self.core.choose_option(q, texts) if not std or self.core.application_answers.contact_kind(q) == 'country_code' else self.core.match_option(cands, texts)
            if idx is None:
                el.press('Escape')
                return False
            self.core.click_form_control(opts.nth(idx))
            self.core.pause(0.3, 0.7)
            return self.core.combobox_value_confirmed(el, q, cands)
        except Exception as e:
            if self.core.browser_closed(e):
                raise self.core.BrowserClosed() from None
            self.core.log(f'    ! could not fill dropdown: {e.__class__.__name__}')
            return False

    def fill_yesno(self, scope: Locator) -> list[str]:
        unanswered = []
        for i, (q, _picked) in enumerate(scope.evaluate(self.core.YESNO_JS)):
            idx = self.core.choose_option(q, ['Yes', 'No']) if q.strip() else None
            if idx is None:
                unanswered.append(q or 'Yes/No question')
                continue
            try:
                group = scope.locator(f"[data-yesno='{i}']")
                before = group.evaluate(self.core.YESNO_SELECTION_JS)
                if before.get('question') != q or len(before.get('states', [])) != 2:
                    unanswered.append(q)
                    continue
                if not self.core.radio_choice_confirmed(before['states'], idx):
                    self.core.click_form_control(group.locator('button').nth(idx))
                self.core.pause(0.3, 0.6)
                after = group.evaluate(self.core.YESNO_SELECTION_JS)
                if after.get('question') != q or len(after.get('states', [])) != 2 or (not self.core.radio_choice_confirmed(after['states'], idx)):
                    unanswered.append(q)
            except Exception as e:
                if self.core.browser_closed(e):
                    raise self.core.BrowserClosed() from None
                unanswered.append(q)
        return unanswered

    def unconfirmed_radio_questions(self, scope: Locator) -> list[str]:
        """Recheck choices after other fields/uploads, without changing any answer."""
        issues = []
        for group in scope.locator(self.core.RADIO_GROUPS).all():
            if self.core.linkedin_forms.resume_group(group):
                if not self.core.linkedin_forms.verified_group(group, self.core):
                    issues.append('Resume selection is not confirmed')
                continue
            q, labels = group.evaluate(self.core.RADIO_GROUP_JS)
            idx = self.core.choose_option(q, labels) if q.strip() else None
            states = group.evaluate(self.core.RADIO_SELECTION_JS)
            if len(states) != len(labels) or not self.core.radio_choice_confirmed(states, idx):
                issues.append(q or 'radio group')
        for i, (q, _picked) in enumerate(scope.evaluate(self.core.YESNO_JS)):
            idx = self.core.choose_option(q, ['Yes', 'No']) if q.strip() else None
            state = scope.locator(f"[data-yesno='{i}']").evaluate(self.core.YESNO_SELECTION_JS)
            if state.get('question') != q or len(state.get('states', [])) != 2 or (not self.core.radio_choice_confirmed(state['states'], idx)):
                issues.append(q or 'Yes/No question')
        issues += scope.locator("input[type='radio'], [role='radio']").evaluate_all('els => els\n        .filter(e => !e.closest("fieldset:has(input[type=\'radio\']), [role=\'radiogroup\']"))\n        .map(e => \'unrecognized radio group: \' + (e.name || e.id || \'unknown question\'))')
        return issues

    def checkbox_choices(self, question: str, options: list[str]) -> list[int] | None:
        """A multi-answer is a complete set, never a best-effort subset."""
        candidates = self.core.answer(question) if question.strip() else []
        if not candidates:
            return None
        picks = [self.core.match_option([candidate], options) for candidate in candidates]
        if any((index is None for index in picks)) or len(set(picks)) != len(picks):
            return None
        return picks

    def checkbox_group_confirmed(self, state, picks) -> bool:
        """Require the exact exclusive set and consistent native/ARIA states."""
        if not isinstance(state, dict) or not isinstance(picks, list) or (not picks):
            return False
        options, states, disabled = (state.get(key) for key in ('options', 'states', 'disabled'))
        if not all((isinstance(values, list) for values in (options, states, disabled))):
            return False
        if len(options) < 2 or len(states) != len(options) or len(disabled) != len(options):
            return False
        if any((type(index) is not int or not 0 <= index < len(options) for index in picks)) or len(set(picks)) != len(picks):
            return False
        return all((type(off) is bool and selected is (index in picks) and (index not in picks or not off) for index, (selected, off) in enumerate(zip(states, disabled))))

    def unconfirmed_checkbox_questions(self, scope: Locator) -> list[str]:
        issues = []
        for i, (q, options, _picked) in enumerate(scope.evaluate(self.core.CHECKGROUP_JS)):
            picks = self.core.checkbox_choices(q, options)
            state = scope.locator(f"[data-checkgroup='{i}']").evaluate(self.core.CHECKGROUP_STATE_JS)
            if state.get('question') != q or state.get('options') != options or (not self.core.checkbox_group_confirmed(state, picks)):
                issues.append(q or 'checkbox group')
        for cb in scope.locator("input[type='checkbox']").all():
            if cb.is_disabled():
                continue
            if self.core.is_optional_follow_checkbox(cb):
                if cb.is_checked():
                    issues.append('company follow preference was not cleared')
                continue
            if cb.evaluate("e => !!e.closest('[data-checkgroup]')"):
                continue
            q = self.core.question_of(cb)
            pick = self.core.choose_option(q, ['Yes', 'No']) if q.strip() else None
            state = cb.evaluate("c => {\n            const aria = c.getAttribute('aria-checked');\n            return c.indeterminate || (aria !== null && aria !== String(c.checked)) ? null : c.checked;\n        }")
            if pick is None or state is not (pick == 0):
                issues.append(q or 'checkbox')
        return issues

    def fill_checkgroups(self, scope: Locator) -> list[str]:
        """Select a complete confirmed set, then verify the whole group, not just the last click."""
        unanswered = []
        for i, (q, opts, _any_checked) in enumerate(scope.evaluate(self.core.CHECKGROUP_JS)):
            picks = self.core.checkbox_choices(q, opts)
            if picks is None:
                unanswered.append(q or 'checkbox group')
                continue
            group = scope.locator(f"[data-checkgroup='{i}']")
            try:
                state = group.evaluate(self.core.CHECKGROUP_STATE_JS)
                if state.get('question') != q or state.get('options') != opts or len(state.get('states', [])) != len(opts) or any((type(value) is not bool for value in state['states'])) or (len(state.get('disabled', [])) != len(opts)) or any((state['disabled'][index] for index in picks)):
                    unanswered.append(q or 'checkbox group')
                    continue
                for index in range(len(opts)):
                    state = group.evaluate(self.core.CHECKGROUP_STATE_JS)
                    if state.get('question') != q or state.get('options') != opts:
                        raise ValueError('checkbox options changed during selection')
                    wanted = index in picks
                    cb = group.locator("input[type='checkbox']").nth(index)
                    if cb.is_checked() == wanted:
                        continue
                    if cb.is_disabled() or cb.evaluate(self.core.NON_SUBMITTING_CONTROL_JS) is not True:
                        raise self.core.UnsafeFormControl('unsafe or disabled checkbox control')
                    try:
                        cb.set_checked(wanted, timeout=2000)
                    except self.core.PWTimeout:
                        if cb.is_checked() != wanted:
                            row = cb.locator("xpath=ancestor::*[normalize-space(.)!=''][1]")
                            self.core.click_form_control(row, timeout=2000)
                            if cb.is_checked() != wanted:
                                cb.set_checked(wanted, force=True, timeout=2000)
                state = group.evaluate(self.core.CHECKGROUP_STATE_JS)
                if state.get('question') != q or state.get('options') != opts or (not self.core.checkbox_group_confirmed(state, picks)):
                    unanswered.append(q or 'checkbox group')
            except Exception as e:
                if self.core.browser_closed(e):
                    raise self.core.BrowserClosed() from None
                self.core.log(f'    ! checkbox selection unconfirmed: {e.__class__.__name__}')
                unanswered.append(q or 'checkbox group')
        return unanswered

    def standard_answer(self, el: Locator, q: str) -> str | None:
        """Resolve standard external fields such as name, email and phone."""
        if self.core.SENSITIVE_RE.search(q) or self.core.NARRATIVE_RE.search(q):
            return None
        if self.core.is_phone_country_control(el, q):
            return self.core.PROFILE.get('country_code') or None
        kind = self.core.application_answers.contact_kind(q)
        if kind == 'country_code':
            return self.core.PROFILE.get('country_code') or None
        if kind == 'phone':
            separate_code = el.evaluate("e => {\n            const root = e.closest('form') || e.closest('[role=dialog]');\n            return root && [...root.querySelectorAll('select,[role=combobox]')].some(c =>\n                (c.id === 'country' && e.id === 'phone' && e.type === 'tel') ||\n                /country (calling )?code/i.test(c.getAttribute('aria-label') || [...(c.labels || [])].map(l=>l.textContent).join(' ')));\n        }")
            return self.core.PROFILE.get('phone' if separate_code is True else 'phone_intl') or None
        key = ' '.join([q, el.get_attribute('name') or '', el.get_attribute('id') or '', el.get_attribute('autocomplete') or '']).lower()
        for pattern, field in self.core.STANDARD_FIELDS:
            if re.search(pattern, key):
                return self.core.PROFILE.get(field) or None
        return None

    def is_phone_country_control(self, el: Locator, question: str) -> bool:
        if self.core.application_answers.question_key(question) != 'country':
            return False
        return el.evaluate("e => e.id === 'country' && e.getAttribute('role') === 'combobox' && !!e.closest('form')?.querySelector('#phone[type=tel]')") is True

    def combobox_value_confirmed(self, el: Locator, question: str, candidates: list[str]) -> bool:
        selected = el.evaluate("e => e.closest('.select__control')?.querySelector('.select__single-value')?.textContent || ''")
        if not selected:
            return self.core.text_value_confirmed(el.input_value(), candidates, question=question)
        if self.core.is_phone_country_control(el, question) or self.core.application_answers.contact_kind(question) == 'country_code':
            return self.core.application_answers.country_code_option(candidates, [selected]) == 0
        return self.core.match_option(candidates, [selected]) == 0

    def text_field_candidates(self, el: Locator, q: str, *, external: bool) -> list[str]:
        if any((key.strip().casefold() == q.strip().casefold() for key in self.core.CFG.get('confirmed_answers', {}))):
            return self.core.answer(q, numeric=el.get_attribute('type') == 'number')
        if self.core.application_answers.preset_answer(q, self.core.SETTINGS):
            return self.core.answer(q, numeric=el.get_attribute('type') == 'number')
        kind = self.core.application_answers.contact_kind(q)
        if kind and (not external):
            return self.core.answer(q)
        std = self.core.standard_answer(el, q) if external else None
        if std:
            return [std]
        numeric = el.get_attribute('type') == 'number' or 'numeric' in (el.get_attribute('id') or '')
        return self.core.answer(q, numeric=numeric)

    def text_value_confirmed(self, value: str, candidates: list[str], *, numeric: bool=False, question: str='') -> bool:
        """Accept only a known value, not merely nonempty/native-valid text."""
        value = value.replace('\r\n', '\n').strip()
        if not value:
            return False
        kind = self.core.application_answers.contact_kind(question)
        if kind == 'email':
            return any((value.casefold() == candidate.strip().casefold() for candidate in candidates))
        if kind == 'phone' and re.fullmatch('[+\\d\\s().-]+', value):
            return any((re.fullmatch('[+\\d\\s().-]+', candidate) and re.sub('\\D', '', value) == re.sub('\\D', '', candidate) for candidate in candidates))
        if numeric:
            number = re.compile('\\d+(?:\\.\\d+)?')
            return bool(number.fullmatch(value)) and any((number.fullmatch(candidate.strip()) and self.core.Decimal(value) == self.core.Decimal(candidate.strip()) for candidate in candidates))
        return any((value == candidate.replace('\r\n', '\n').strip() for candidate in candidates))

    def unconfirmed_text_questions(self, scope: Locator, *, external: bool) -> list[str]:
        """Read values again after other fields/uploads; never silently repair prefills."""
        issues = []
        for el in scope.locator(self.core.TEXT_FIELDS).all():
            if el.is_disabled():
                continue
            q = self.core.question_of(el)
            candidates = self.core.text_field_candidates(el, q, external=external)
            if external and el.get_attribute('role') == 'combobox':
                confirmed = self.core.combobox_value_confirmed(el, q, candidates)
            else:
                confirmed = self.core.text_value_confirmed(el.input_value(), candidates, numeric=el.get_attribute('type') == 'number', question=q)
            if not confirmed:
                issues.append(q or 'text field')
        return issues

    def fill_step(self, page: Page, modal: Locator, external: bool=False) -> list[str]:
        """Count actual form access/changed values without logging personal values."""
        self.core.METRICS.mark('forms_opened')
        if external:
            self.core.ensure_application_scope(modal)
        try:
            before = modal.evaluate(self.core.FORM_VALUES_JS)
        except Exception:
            before = None
        result = self.core._fill_step(page, modal, external)
        if before is not None:
            try:
                after = modal.evaluate(self.core.FORM_VALUES_JS)
                if after != before:
                    self.core.METRICS.mark('forms_changed')
            except Exception:
                pass
        return result

    def _fill_step(self, page: Page, modal: Locator, external: bool=False) -> list[str]:
        """Fill empty fields and verify answers; nonempty text is not confirmation."""
        unanswered: list[str] = []
        if external:
            self.core.ensure_application_scope(modal)
        else:
            unanswered += self.core.linkedin_forms.prepare_resume(page, modal, self.core)
        fields = modal.locator(self.core.TEXT_FIELDS)
        if external:
            controls = self.core.stable_application_controls(modal, self.core.TEXT_FIELDS)
        else:
            controls = [(el, None) for el in fields.all()]
        for el, expected_question in controls:
            try:
                if external:
                    self.core.ensure_application_scope(modal, el, question=expected_question)
                if not el.is_editable(timeout=2000):
                    continue
                q = self.core.question_of(el)
                if external and q != expected_question:
                    raise self.core.ApplicationFormChanged('application_field_question_changed')
                cands = self.core.text_field_candidates(el, q, external=external)
                numeric = el.get_attribute('type') == 'number'
                if external and el.get_attribute('role') == 'combobox':
                    if not self.core.combobox_value_confirmed(el, q, cands) and (not self.core.fill_combobox(page, el, q)):
                        unanswered.append(q)
                    continue
                if el.input_value().strip():
                    if self.core.text_value_confirmed(el.input_value(), cands, numeric=numeric, question=q):
                        continue
                    replace_contact = self.core.SETTINGS.get('replace_prefilled_contacts') is True and self.core.application_answers.contact_kind(q) in ('first_name', 'last_name', 'full_name', 'email', 'phone') and bool(cands)
                    if not replace_contact:
                        unanswered.append(q or 'text field')
                        continue
                    self.core.log('    .. replacing a prefilled contact with the reviewed profile value (enabled in GUI)')
                if not cands:
                    self.core.log(f'    ? unanswered question: {q[:90]}')
                    unanswered.append(q)
                    continue
                el.fill(cands[0])
                self.core.pause(0.6, 1.2)
                sugg = page.locator("[role='listbox'] [role='option']:visible")
                if sugg.count():
                    idx = self.core.match_option(cands, sugg.all_inner_texts())
                    if idx is None:
                        unanswered.append(q)
                    else:
                        self.core.click_form_control(sugg.nth(idx))
                if not self.core.text_value_confirmed(el.input_value(), cands, numeric=numeric, question=q):
                    unanswered.append(q or 'text field')
            except Exception as e:
                if isinstance(e, self.core.ApplicationFormChanged):
                    raise
                if self.core.browser_closed(e):
                    raise self.core.BrowserClosed() from None
                if external:
                    self.core.ensure_application_scope(modal, el, question=expected_question)
                try:
                    q = self.core.question_of(el)[:70]
                except Exception:
                    q = '?'
                self.core.log(f'    ! field could not be filled ({e.__class__.__name__}): {q}')
                unanswered.append(q or 'text field')
        if external:
            self.core.ensure_application_scope(modal)
        for sel in modal.locator('select:visible').all():
            try:
                options = sel.locator('option').all_inner_texts()
                idx = self.core.choose_option(self.core.question_of(sel), options)
                if idx is not None:
                    sel.select_option(index=idx)
                    if sel.locator('option').all_inner_texts() != options or sel.evaluate('e => e.selectedIndex') != idx:
                        unanswered.append(self.core.question_of(sel) or 'select value changed')
                else:
                    unanswered.append(self.core.question_of(sel) or 'select')
            except Exception as e:
                self.core.log(f'    ! could not fill select: {e.__class__.__name__}')
                unanswered.append(self.core.question_of(sel) or 'select')
        unanswered += self.core.fill_radios(modal)
        unanswered += self.core.fill_checkgroups(modal)
        for cb in modal.locator("input[type='checkbox']").all():
            if cb.is_disabled():
                continue
            if self.core.is_optional_follow_checkbox(cb):
                if not self.core.clear_follow_preference(cb):
                    unanswered.append('company follow preference was not cleared')
                continue
            if not cb.is_visible():
                continue
            if cb.evaluate("e => !!e.closest('[data-checkgroup]')"):
                continue
            q = self.core.question_of(cb)
            idx = self.core.choose_option(q, ['Yes', 'No'])
            if idx is None:
                unanswered.append(q or 'checkbox')
                continue
            try:
                cb.set_checked(idx == 0)
            except Exception:
                unanswered.append(q or 'checkbox')
        resume = self.core.HOME / self.core.PROFILE['resume_file']
        if external:
            self.core.ensure_application_scope(modal)
            unanswered += self.core.fill_yesno(modal)
            for fi, expected_question in self.core.stable_application_controls(modal, "input[type='file']"):
                self.core.ensure_application_scope(modal, fi, question=expected_question)
                if self.core.question_of(fi) != expected_question:
                    raise self.core.ApplicationFormChanged('application_upload_question_changed')
                key = (self.core.question_of(fi) + ' ' + (fi.get_attribute('name') or '') + ' ' + (fi.get_attribute('id') or '')).lower()
                if re.search('cover', key) or not resume.exists():
                    continue
                if re.search('resume|\\bcv\\b|lebenslauf|özgeçmiş', key) or modal.locator("input[type='file']").count() == 1:
                    try:
                        if fi.evaluate('(e, name) => e.files.length === 1 && e.files[0].name === name', resume.name):
                            continue
                        fi.set_input_files(str(resume))
                        self.core.pause(2, 3)
                    except Exception as e:
                        self.core.log(f'    ! could not upload CV: {e.__class__.__name__}')
                        unanswered.append('resume upload')
            return unanswered
        return unanswered

    def has_errors(self, modal: Locator) -> bool:
        if modal.locator("[aria-invalid='true']:visible, .artdeco-inline-feedback--error:visible, [data-test-form-element-error-messages]:visible").count():
            return True
        # LinkedIn announces a successful upload as role=alert. It is not a
        # validation error and must not send the new preview down the stalled-
        # Next branch before the final step clears Follow. Unknown alerts still
        # fail closed; explicit error markers above always take precedence.
        return any(' '.join(text.split()).casefold() != 'resume uploaded successfully'
                   for text in modal.locator("[role='alert']:visible").all_inner_texts())

    def native_form_issues(self, form: Locator) -> list[str]:
        """Native/ARIA validity still applies to user-assisted, explicitly reviewed steps."""
        issues = form.locator("input, textarea, select, [aria-required='true']:visible").evaluate_all('els => els.filter(e => {\n            if (e.disabled || e.readOnly || e.type === \'hidden\') return false;\n            if (e.validity && !e.validity.valid) return true;\n            if (e.getAttribute(\'aria-invalid\') === \'true\') return true;\n            if (e.getAttribute(\'aria-required\') !== \'true\') return false;\n            if (e.type === \'checkbox\' || e.type === \'radio\') return !e.checked;\n            if (e.type === \'file\') return !e.files.length;\n            if (e.getAttribute(\'role\') === \'radiogroup\')\n                return !e.querySelector(\'[aria-checked="true"], input:checked\');\n            if (\'value\' in e) return !e.value.trim();\n            return !e.getAttribute(\'aria-valuetext\');\n        }).map(e => e.getAttribute(\'aria-label\') || e.name || e.id || \'invalid required field\')')
        return issues
