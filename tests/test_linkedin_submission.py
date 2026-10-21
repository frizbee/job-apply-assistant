"""Explicit LinkedIn confirmation and durable no-blind-retry regression tests."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from test_answer_safety import SafetyTest, node
import apply


class ConfirmationTests(SafetyTest):
    def setUp(self):
        super().setUp()
        self.page, self.modal = node(), node()
        self.page.url = "https://www.linkedin.com/jobs/view/123/"
        self.page.locator.return_value.all_inner_texts.return_value = []
        self.modal.count.return_value = 0
        self.modal.all_inner_texts.return_value = []

    def test_only_explicit_confirmation_lines_are_accepted(self):
        self.page.locator.return_value.all_inner_texts.return_value = [
            "Application sent\nDone", "Your application was sent to Example.",
            "Application submitted successfully!", "Başvurunuz gönderildi.",
            "Thanks for your interest", "Submit application", "You have not applied", "application failed",
        ]
        signals = apply.linkedin_submission_signals(self.page)
        self.assertEqual(len(signals), 4)
        self.assertIn("application sent", signals)

    def test_delayed_confirmation_is_awaited(self):
        with patch.object(apply, "linkedin_submission_signals", side_effect=[set(), {"application sent"}]):
            self.assertEqual(apply.wait_linkedin_submission(self.page, self.modal, set()), "applied")
        apply.pause.assert_called_once()

    def test_timeout_or_closed_modal_is_not_success(self):
        self.assertEqual(apply.wait_linkedin_submission(self.page, self.modal, set(), timeout_seconds=0),
                         "submission_unconfirmed")

    def test_stale_confirmation_is_not_success(self):
        with patch.object(apply, "linkedin_submission_signals", return_value={"application sent"}):
            self.assertEqual(apply.wait_linkedin_submission(self.page, self.modal, {"application sent"}, 0),
                             "submission_unconfirmed")

    def test_validation_error_is_rejected_not_applied(self):
        self.modal.count.return_value = 1
        self.assertEqual(apply.wait_linkedin_submission(self.page, self.modal, set(), 0), "submission_rejected")

    def test_network_error_alert_is_uncertain_not_definite_rejection(self):
        self.modal.all_inner_texts.return_value = ["Something went wrong. Please try again."]
        self.assertEqual(apply.wait_linkedin_submission(self.page, self.modal, set(), 0), "submission_unconfirmed")

    def test_navigation_to_other_job_does_not_confirm_this_job(self):
        with patch.object(apply, "linkedin_submission_signals", return_value={"application sent"}):
            self.assertEqual(apply.wait_linkedin_submission(self.page, self.modal, set(), 0,
                             expected_url="https://www.linkedin.com/jobs/view/456/"), "submission_unconfirmed")


class PersistenceTests(SafetyTest):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.object(apply, "DRY_RUN", False))
        home = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="linkedin-submit-")))
        self.home = home
        self.enterContext(patch.object(apply, "APPLIED_FILE", home / "applied.json"))
        self.enterContext(patch.object(apply, "LOG_FILE", home / "applications.csv"))
        self.enterContext(patch.object(apply, "MANUAL_FILE", home / "manual_apply.csv"))
        self.enterContext(patch.object(apply, "linkedin_submission_signals", return_value=set()))
        self.wait = self.enterContext(patch.object(apply, "wait_linkedin_submission", return_value="applied"))
        self.page, self.modal, self.primary = node(), node(), node()
        self.page.url = "https://www.linkedin.com/jobs/view/123/"
        self.state = {}

    def submit(self):
        return apply.submit_linkedin(self.page, self.modal, self.primary, "123", "Engineer", "Example", self.state)

    def test_pending_is_durable_before_click_and_success_is_confirmed(self):
        def click():
            self.assertEqual(apply.load_applied()["123"]["status"], "submission_pending")
            self.assertEqual(self.state["123"]["status"], "submission_pending")
        self.primary.click.side_effect = click
        self.assertEqual(self.submit(), "applied")
        self.assertEqual(apply.load_applied()["123"]["status"], "applied")
        self.assertFalse(apply.MANUAL_FILE.exists())
        self.assertFalse(apply.APPLIED_FILE.with_suffix(".tmp").exists())

    def test_easy_apply_live_submit_uses_confirmation_and_shared_history(self):
        empty = node()
        empty.count.return_value = 0
        self.primary.inner_text.return_value = "Submit application"
        self.primary.evaluate.return_value = True
        self.page.get_by_role.return_value = self.primary
        self.page.locator.return_value = self.modal
        self.modal.locator.side_effect = lambda selector: self.primary if "footer button" in selector else empty
        with patch.object(apply, "fill_step", return_value=[]), patch.object(apply, "require_manual_review", return_value=False):
            self.assertEqual(apply.apply_to_job(self.page, "123", "Engineer", "Example", self.state), "applied")
        self.wait.assert_called_once()
        self.assertEqual(self.state["123"]["status"], "applied")

    def test_failed_atomic_save_does_not_click_or_destroy_previous_history(self):
        apply.record(self.state, "old", "Other job", "Example", "applied")
        with patch.object(Path, "replace", side_effect=OSError("disk error")):
            with self.assertRaises(OSError):
                self.submit()
        self.primary.click.assert_not_called()
        self.assertEqual(set(apply.load_applied()), {"old"})

    def test_unknown_and_rejected_results_are_saved_for_manual_check(self):
        for status in ["submission_unconfirmed", "submission_rejected"]:
            with self.subTest(status=status):
                self.state.clear()  # Independent attempts, not a retry of the previous subcase.
                self.wait.return_value = status
                self.assertEqual(self.submit(), status)
                self.assertEqual(apply.load_applied()["123"]["status"], status)
                self.assertIn(status, apply.MANUAL_FILE.read_text())
                self.assertFalse(apply.can_retry(status))

    def test_click_timeout_never_becomes_success(self):
        self.primary.click.side_effect = apply.PWTimeout("click timed out after dispatch")
        self.assertEqual(self.submit(), "submission_unconfirmed")
        self.wait.assert_not_called()
        self.assertEqual(apply.load_applied()["123"]["status"], "submission_unconfirmed")

    def test_stop_or_closed_browser_preserves_uncertain_result(self):
        for error in [apply.Stopped(), apply.BrowserClosed(), RuntimeError("Target closed")]:
            with self.subTest(error=error):
                self.state.clear()
                self.wait.side_effect = error
                with self.assertRaises(apply.Stopped):
                    self.submit()
                self.assertEqual(apply.load_applied()["123"]["status"], "submission_unconfirmed")
                self.assertIn("submission_unconfirmed", apply.MANUAL_FILE.read_text())

    def test_cleanup_error_does_not_erase_confirmed_success(self):
        self.page.keyboard.press.side_effect = RuntimeError("Target closed")
        self.assertEqual(self.submit(), "applied")
        self.assertEqual(apply.load_applied()["123"]["status"], "applied")

    def test_uncertain_attempt_is_not_repeated_on_another_source(self):
        for status in ["submission_pending", "submission_unconfirmed"]:
            apply.record(self.state, "123", "Engineer", "Example", status)
            self.assertTrue(apply.applied_elsewhere(self.state, "Engineer", "Example"))
            self.assertFalse(apply.can_retry(status))
        apply.record(self.state, "123", "Engineer", "Example", "submission_rejected")
        self.assertFalse(apply.applied_elsewhere(self.state, "Engineer", "Example"))

    def test_hard_process_exit_leaves_pending_record(self):
        script = """
import os
from types import SimpleNamespace
import apply
apply.linkedin_submission_signals = lambda page: set()
apply.submit_linkedin(SimpleNamespace(url='https://www.linkedin.com/jobs/view/123/'), None,
    SimpleNamespace(click=lambda: os._exit(7)), '123', 'Engineer', 'Example', {})
"""
        result = subprocess.run([sys.executable, "-c", script], cwd=Path(apply.__file__).parent,
                                env={**os.environ, "AUTOAPPLY_HOME": str(self.home)}, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 7, result.stderr.decode())
        saved = json.loads((self.home / "data" / "applied.json").read_text())
        self.assertEqual(saved["123"]["status"], "submission_pending")
        self.assertTrue(apply.applied_elsewhere(saved, "Engineer", "Example"))


@unittest.skipUnless(os.getenv("AUTOAPPLY_BROWSER_TESTS") == "1", "optional local Chrome DOM tests")
class BrowserConfirmationTests(SafetyTest):
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
        self.page.set_content('<div role="dialog"><button id="submit" onclick="this.parentElement.innerHTML=\'Application sent\'">Submit</button></div>')
        self.modal = self.page.locator('[role="dialog"]')

    def test_new_confirmation_is_recognized_from_real_dom(self):
        before = apply.linkedin_submission_signals(self.page)
        self.page.locator("#submit").click()
        self.assertEqual(apply.wait_linkedin_submission(self.page, self.modal, before, 0), "applied")

    def test_hidden_or_unrelated_text_is_not_a_confirmation(self):
        self.page.set_content('<p>Application sent</p><div role="status" style="display:none">Application sent</div><div role="dialog">Submit application</div>')
        self.assertEqual(apply.linkedin_submission_signals(self.page), set())

    def test_visible_validation_error_is_not_success(self):
        self.modal.evaluate("e => e.innerHTML = '<input aria-invalid=\"true\"><p role=\"alert\">Required</p>'")
        self.assertEqual(apply.wait_linkedin_submission(self.page, self.modal, set(), 0), "submission_rejected")


if __name__ == "__main__":
    unittest.main()
