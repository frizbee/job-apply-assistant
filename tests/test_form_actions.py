"""Final-review regressions: only explicit guarded actions may submit a form."""
import os
from unittest.mock import patch
import unittest

from test_answer_safety import SafetyTest, node
import apply
import sources


class ActionClassificationTests(SafetyTest):
    def button(self, text, aria=""):
        button = node()
        button.inner_text.return_value = text
        button.get_attribute.return_value = aria
        return button

    def test_only_known_exact_navigation_labels_are_allowed(self):
        for text in ["Next", "Continue", "Review your application", "Continue to next step", "İleri", "Devam et"]:
            self.assertEqual(apply.application_button_action(self.button(text)), "next")
        for text in ["Отправить заявку", "Continue and submit", "Next: send application", "Finish", ""]:
            self.assertEqual(apply.application_button_action(self.button(text)), "unknown")

    def test_final_actions_are_explicit_and_conflicting_labels_are_unknown(self):
        for text in ["Submit", "Submit application", "Submit your application", "Send application", "Başvurunu gönder"]:
            self.assertEqual(apply.application_button_action(self.button(text)), "submit")
        self.assertEqual(apply.application_button_action(self.button("Next", "Submit application")), "unknown")
        self.assertEqual(apply.application_button_action(self.button("Submit", "Unrecognized action")), "unknown")

    def test_matching_text_and_aria_or_whitespace_do_not_conflict(self):
        self.assertEqual(apply.application_button_action(self.button(" Continue ", "Continue to next step")), "next")
        self.assertEqual(apply.application_button_action(self.button("Submit", "Submit application")), "submit")

    def test_central_submit_guard_also_enforces_dry_run(self):
        button = node()
        with patch.object(apply, "DRY_RUN", True), patch.object(apply, "record") as record, \
                patch.object(apply, "load_applied") as history:
            self.assertEqual(apply.submit_application(node(), button, "fixture", "Engineer", "Example",
                             "https://example.com/job", lambda: "applied"), "dry_run")
        button.click.assert_not_called()
        record.assert_not_called()
        history.assert_not_called()

    def test_unsafe_fill_control_never_reaches_click(self):
        control = node()
        control.evaluate.return_value = False
        with self.assertRaises(apply.UnsafeFormControl):
            apply.click_form_control(control)
        control.click.assert_not_called()


@unittest.skipUnless(os.getenv("AUTOAPPLY_BROWSER_TESTS") == "1", "optional local Chrome form-action tests")
class BrowserFormActionTests(SafetyTest):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.pw = sync_playwright().start()
        cls.addClassCleanup(cls.pw.stop)
        cls.browser = cls.pw.chromium.launch(channel="chrome", headless=True)
        cls.addClassCleanup(cls.browser.close)

    def setUp(self):
        super().setUp()
        self.context = self.browser.new_context()
        self.addCleanup(self.context.close)
        self.context.route("**/*", lambda route: route.abort())
        self.page = self.context.new_page()
        self.page.set_default_timeout(1000)
        self.manual = self.enterContext(patch.object(apply, "record_manual"))
        self.record = self.enterContext(patch.object(apply, "record"))
        self.enterContext(patch.object(apply, "DRY_RUN", True))
        self.question = "Are you willing to travel?"
        apply.CFG["confirmed_answers"] = {self.question: "Yes"}

    def form(self, content, modal=False):
        html = '<script>window.sent=0; window.next=0; window.choice=""</script>'
        if modal:
            html += '<button type="button">Easy Apply</button><div role="dialog">'
        html += '<form onsubmit="event.preventDefault(); window.sent++">' + content + '</form>'
        if modal:
            html += '</div>'
        self.page.set_content(html)
        return self.page.locator("form")

    def test_unknown_footer_does_not_submit_in_either_mode(self):
        for dry_run in [True, False]:
            with self.subTest(dry_run=dry_run), patch.object(apply, "DRY_RUN", dry_run):
                self.form('<footer><button type="submit">Отправить заявку</button></footer>', modal=True)
                result = apply.apply_to_job(self.page, "fixture", "Engineer", "Example")
                self.assertEqual(self.page.evaluate("window.sent"), 0)
                self.assertEqual(result, "manual_review")
        self.record.assert_not_called()
        self.assertEqual(self.manual.call_count, 2)

    def test_yesno_default_buttons_do_not_submit_in_either_mode(self):
        for dry_run in [True, False]:
            with self.subTest(dry_run=dry_run), patch.object(apply, "DRY_RUN", dry_run):
                form = self.form(f'<p>{self.question}</p><div><button>Yes</button><button>No</button></div>')
                unanswered = apply.fill_yesno(form)
                self.assertEqual(self.page.evaluate("window.sent"), 0)
                self.assertEqual(unanswered, [self.question])
        self.record.assert_not_called()

    def test_yesno_submit_or_invalid_type_buttons_are_not_clicked(self):
        for button_type in ["submit", "invalid"]:
            form = self.form(f'<p>{self.question}</p><div><button type="{button_type}">Yes</button><button type="button">No</button></div>')
            self.assertEqual(apply.fill_yesno(form), [self.question])
            self.assertEqual(self.page.evaluate("window.sent"), 0)

    def test_explicit_non_submit_yesno_still_selects_answer(self):
        form = self.form(f'<p>{self.question}</p><div><button type="button" aria-pressed="false" onclick="window.choice=\'Yes\';this.setAttribute(\'aria-pressed\',\'true\')">Yes</button>'
                         '<button type="button" aria-pressed="false">No</button></div>')
        self.assertEqual(apply.fill_yesno(form), [])
        self.assertEqual(self.page.evaluate("window.choice"), "Yes")
        self.assertEqual(self.page.evaluate("window.sent"), 0)

    def test_known_submit_does_not_click_in_dry_run_and_live_uses_guard(self):
        self.form('<footer><button type="submit">Send application</button></footer>', modal=True)
        self.assertEqual(apply.apply_to_job(self.page, "fixture"), "dry_run")
        self.assertEqual(self.page.evaluate("window.sent"), 0)
        with patch.object(apply, "DRY_RUN", False), patch.object(apply, "submit_linkedin", return_value="applied") as submit:
            self.assertEqual(apply.apply_to_job(self.page, "fixture"), "applied")
        submit.assert_called_once()
        self.record.assert_not_called()

    def test_known_next_then_submit_is_reclassified_before_click(self):
        self.form('''<footer><button type="button" onclick="window.next++; this.outerHTML='<button type=submit>Send application</button>'">Next</button></footer>''', modal=True)
        self.assertEqual(apply.apply_to_job(self.page, "fixture"), "dry_run")
        self.assertEqual(self.page.evaluate("window.next"), 1)
        self.assertEqual(self.page.evaluate("window.sent"), 0)

    def test_aria_only_next_still_uses_safe_navigation(self):
        self.form('''<footer><button type="button" aria-label="Next" onclick="window.next++; this.outerHTML='<button type=submit>Submit application</button>'"></button></footer>''', modal=True)
        self.assertEqual(apply.apply_to_job(self.page, "fixture"), "dry_run")
        self.assertEqual(self.page.evaluate("window.next"), 1)
        self.assertEqual(self.page.evaluate("window.sent"), 0)

    def test_retry_cannot_click_new_submit_as_old_next(self):
        self.form('''<footer><button type="button" onclick="window.next++; this.outerHTML='<button type=submit>Submit application</button>'">Next</button></footer>''', modal=True)
        with patch.object(apply, "has_errors", return_value=True):
            self.assertEqual(apply.apply_to_job(self.page, "fixture"), "dry_run")
        self.assertEqual(self.page.evaluate("window.sent"), 0)

    def test_native_submit_disguised_as_navigation_is_manual(self):
        self.form('<footer><button type="submit">Next</button></footer>', modal=True)
        self.assertEqual(apply.apply_to_job(self.page, "fixture"), "manual_review")
        self.assertEqual(self.page.evaluate("window.sent"), 0)

    def test_manual_fallback_does_not_submit_via_discard_button(self):
        self.form('<footer><button>Discard</button><button type="submit">Отправить заявку</button></footer>', modal=True)
        self.assertEqual(apply.apply_to_job(self.page, "fixture"), "manual_review")
        self.assertEqual(self.page.evaluate("window.sent"), 0)

    def test_custom_radio_submit_buttons_cannot_send_during_fill(self):
        form = self.form(f'<p>{self.question}</p><div role="radiogroup">'
                         '<button role="radio">Yes</button><button role="radio">No</button></div>')
        self.assertEqual(apply.fill_radios(form), [self.question])
        self.assertEqual(self.page.evaluate("window.sent"), 0)

    def test_submit_images_labels_children_and_wrappers_are_rejected(self):
        cases = [('<input type="image" alt="Choice">', 'input'),
                 ('<button><span role="option">Choice</span></button>', '[role="option"]'),
                 ('<label for="send">Choice</label><button id="send">Send</button>', 'label'),
                 ('<div role="option"><button type="submit">Choice</button></div>', '[role="option"]')]
        for html, selector in cases:
            with self.subTest(html=html):
                form = self.form(html)
                with self.assertRaises(apply.UnsafeFormControl):
                    apply.click_form_control(form.locator(selector))
                self.assertEqual(self.page.evaluate("window.sent"), 0)

    def test_native_type_change_after_inspection_does_not_submit(self):
        form = self.form('<button type="button">Yes</button>')
        button = form.locator('button')
        original = button.evaluate
        def inspect_then_change(*args, **kwargs):
            result = original(*args, **kwargs)
            original("el => el.type = 'submit'")
            return result
        with patch.object(button, "evaluate", side_effect=inspect_then_change):
            with self.assertRaises(apply.PWTimeout):
                apply.click_form_control(button, timeout=200)
        self.assertEqual(self.page.evaluate("window.sent"), 0)

    def test_indeed_rejects_ambiguous_and_submit_capable_navigation(self):
        job = {"id": "indeed:fixture", "title": "Engineer", "company": "Example", "url": "https://www.indeed.com/viewjob?jk=fixture"}
        for text in ["Continue and submit", "Next"]:
            with self.subTest(text=text), patch.object(sources, "core", apply), patch.object(sources, "goto", return_value=True):
                self.form('<button id="indeedApplyButton" type="button">Easy Apply</button>'
                          f'<main><button type="submit">{text}</button></main>')
                self.assertEqual(sources.indeed_apply(self.page, job), "manual_review")
                self.assertEqual(self.page.evaluate("window.sent"), 0)

    def test_native_submit_openers_are_not_clicked(self):
        for board, label in [("linkedin", "Easy Apply"), ("indeed", "Easy Apply"), ("wellfound", "Apply")]:
            with self.subTest(board=board):
                self.form(f'<button id="indeedApplyButton">{label}</button>')
                if board == "linkedin":
                    status = apply.apply_to_job(self.page, "fixture")
                else:
                    job = {"id": board + ":fixture", "title": "Engineer", "company": "Example", "url": f"https://{board}.com/job"}
                    with patch.object(sources, "core", apply), patch.object(sources, "goto", return_value=True), \
                            patch.object(sources, "wellfound_login", return_value=True):
                        status = getattr(sources, board + "_apply")(self.page, job)
                self.assertEqual(status, "manual_review")
                self.assertEqual(self.page.evaluate("window.sent"), 0)
