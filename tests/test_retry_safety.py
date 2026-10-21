"""No blind retries: shared submission guard, legacy migration and all submitters."""
import csv
import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from test_answer_safety import SafetyTest, node
import apply
import sources


class RetryPolicyTests(SafetyTest):
    def test_only_explicit_pre_submit_statuses_allow_retry(self):
        for status in apply.RETRYABLE_STATUSES:
            self.assertTrue(apply.can_retry(status))
        for status in ["skipped_unconfirmed", "skipped_error", "skipped_new_unknown_reason", "external_unconfirmed",
                       "submission_pending", "submission_unconfirmed", "submission_rejected", "applied", "manual_review"]:
            self.assertFalse(apply.can_retry(status))

    def test_no_easy_apply_depends_on_external_support(self):
        for enabled in [False, True]:
            with patch.object(apply, "EXTERNAL", enabled):
                self.assertEqual(apply.can_retry("no_easy_apply"), enabled)

    def test_legacy_uncertainty_already_blocks_cross_source_duplicates(self):
        for status in apply.LEGACY_UNCERTAIN_STATUSES:
            state = {"indeed:old": {"status": status, "key": apply.job_key("Engineer", "Example")}}
            self.assertTrue(apply.applied_elsewhere(state, "Engineer", "Example"))


class HistoryTestBase(SafetyTest):
    def setUp(self):
        super().setUp()
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="retry-history-")))
        self.enterContext(patch.object(apply, "APPLIED_FILE", self.home / "applied.json"))
        self.enterContext(patch.object(apply, "LOG_FILE", self.home / "applications.csv"))
        self.enterContext(patch.object(apply, "MANUAL_FILE", self.home / "manual_apply.csv"))


class HistoryTests(HistoryTestBase):
    def test_legacy_migration_preserves_audit_details_and_is_idempotent(self):
        original = {"indeed:old": {"status": "skipped_unconfirmed", "date": "2026-10-01", "key": "exampleengineer"},
                    "done": {"status": "applied", "date": "2026-10-01", "key": "other"}}
        apply.APPLIED_FILE.write_text(json.dumps(original))
        with apply.LOG_FILE.open("w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["job_id", "title", "company", "url"])
            writer.writerow(["indeed:old", "Engineer", "Example", "https://www.indeed.com/viewjob?jk=old"])
        migrated = apply.load_applied()
        self.assertEqual(migrated["indeed:old"]["status"], "submission_unconfirmed")
        self.assertEqual(migrated["indeed:old"]["legacy_status"], "skipped_unconfirmed")
        self.assertEqual(migrated["indeed:old"]["date"], "2026-10-01")
        self.assertEqual(migrated["done"], original["done"])
        self.assertEqual(apply.load_applied(), migrated)
        with apply.MANUAL_FILE.open(newline="") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["apply_url"], "https://www.indeed.com/viewjob?jk=old")

    def test_all_legacy_ambiguous_statuses_are_held_for_manual_review(self):
        apply.APPLIED_FILE.write_text(json.dumps({str(i): {"status": status, "key": "key"}
                                                  for i, status in enumerate(apply.LEGACY_UNCERTAIN_STATUSES)}))
        migrated = apply.load_applied()
        self.assertTrue(all(v["status"] == "submission_unconfirmed" for v in migrated.values()))
        self.assertEqual(len(migrated), len(apply.LEGACY_UNCERTAIN_STATUSES))

    def test_corrupt_or_invalid_history_does_not_become_empty_history(self):
        for content in ["{broken", "[]", '{"job": {}}', '{"job": {"status": 12}}']:
            apply.APPLIED_FILE.write_text(content)
            with self.assertRaises(RuntimeError):
                apply.load_applied()
            self.assertEqual(apply.APPLIED_FILE.read_text(), content)

    def test_missing_history_is_valid_empty_state(self):
        self.assertEqual(apply.load_applied(), {})

    def test_generic_guard_persists_pending_before_click_and_catches_timeout(self):
        state = {}
        page, button = node(), node()
        def click():
            self.assertEqual(apply.load_applied()["indeed:1"]["status"], "submission_pending")
            self.assertEqual(state["indeed:1"]["status"], "submission_pending")
            raise apply.PWTimeout("click may have reached the server")
        button.click.side_effect = click
        result = apply.submit_application(page, button, "indeed:1", "Engineer", "Example",
                                           "https://www.indeed.com/viewjob?jk=1", lambda: "applied", state)
        self.assertEqual(result, "submission_unconfirmed")
        self.assertEqual(state["indeed:1"]["status"], result)
        self.assertTrue(apply.applied_elsewhere(state, "Engineer", "Example"))

    def test_direct_second_submit_is_blocked_even_without_driver_gate(self):
        page, button = node(), node()
        state = {}
        args = (page, button, "indeed:1", "Engineer", "Example", "https://www.indeed.com/viewjob?jk=1")
        self.assertEqual(apply.submit_application(*args, lambda: "submission_unconfirmed", state), "submission_unconfirmed")
        self.assertEqual(apply.submit_application(*args, lambda: "applied", state), "submission_unconfirmed")
        button.click.assert_called_once()

    def test_prior_success_is_not_resubmitted_or_counted_as_a_new_application(self):
        page, button = node(), node()
        state = {"wellfound:1": {"status": "applied"}}
        self.assertEqual(apply.submit_application(page, button, "wellfound:1", "Engineer", "Example",
            "https://wellfound.com/jobs/1", lambda: "applied", state), "applied_previously")
        button.click.assert_not_called()

    def test_hard_process_crash_with_non_linkedin_id_preserves_pending(self):
        script = """
import os
from types import SimpleNamespace
import apply
apply.submit_application(None, SimpleNamespace(click=lambda: os._exit(7)), 'indeed:job',
    'Engineer', 'Example', 'https://www.indeed.com/viewjob?jk=job', lambda: 'applied', {})
"""
        result = subprocess.run([sys.executable, "-c", script], cwd=Path(apply.__file__).parent,
                                env={**os.environ, "AUTOAPPLY_HOME": str(self.home)}, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 7, result.stderr.decode())
        state = json.loads((self.home / "data" / "applied.json").read_text())
        self.assertEqual(state["indeed:job"]["status"], "submission_pending")
        self.assertEqual(state["indeed:job"]["url"], "https://www.indeed.com/viewjob?jk=job")


class SubmitterTests(HistoryTestBase):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.object(sources, "core", apply))
        self.enterContext(patch.object(sources, "goto", return_value=True))
        self.enterContext(patch.object(sources, "wellfound_login", return_value=True))
        self.enterContext(patch.object(apply, "wait_human", return_value=True))
        self.enterContext(patch.object(apply, "require_manual_review", return_value=False))
        self.enterContext(patch.object(apply, "fill_step", return_value=[]))
        self.enterContext(patch.object(apply, "application_submission_signals", return_value=set()))
        self.enterContext(patch.object(apply, "DRY_RUN", False))
        self.state = {}

    def board_submit(self, board, outcome):
        page, form, send = node(), node(), node()
        page.url = f"https://{board}.com/jobs/123"
        page.context.pages = [page]
        page.locator.return_value = form
        page.get_by_role.return_value = node() if board == "wellfound" else send
        form.get_by_role.return_value = send
        send.inner_text.return_value = "Submit your application"
        job = {"id": f"{board}:123", "title": "Engineer", "company": "Example", "url": page.url}
        with patch.object(apply, "wait_application_submission", side_effect=outcome if isinstance(outcome, BaseException) else None,
                          return_value=outcome):
            status = getattr(sources, board + "_apply")(page, job, self.state)
        return status, send

    def test_both_boards_preserve_uncertainty_in_shared_state(self):
        for board in ["indeed", "wellfound"]:
            status, send = self.board_submit(board, "submission_unconfirmed")
            self.assertEqual(status, "submission_unconfirmed")
            self.assertEqual(self.state[f"{board}:123"]["status"], status)
            send.click.assert_called_once()

    def test_both_boards_preserve_confirmation_errors_not_skipped_error(self):
        for board in ["indeed", "wellfound"]:
            status, _ = self.board_submit(board, apply.PWTimeout("lost response"))
            self.assertEqual(status, "submission_unconfirmed")
            self.assertFalse(apply.can_retry(status))

    def test_both_boards_stopped_after_submit_keep_uncertain_history(self):
        for board in ["indeed", "wellfound"]:
            with self.assertRaises(apply.Stopped):
                self.board_submit(board, apply.Stopped())
            self.assertEqual(self.state[f"{board}:123"]["status"], "submission_unconfirmed")

    def test_confirmed_success_still_works_on_both_boards(self):
        for board in ["indeed", "wellfound"]:
            status, _ = self.board_submit(board, "applied")
            self.assertEqual(status, "applied")
            self.assertEqual(self.state[f"{board}:123"]["status"], "applied")

    def test_external_forms_use_same_guard_for_uncertain_and_confirmed_results(self):
        for number, outcome in enumerate(["submission_unconfirmed", "applied"]):
            page, form = node(), node()
            page.url = "https://jobs.lever.co/example/id/apply"
            page.goto.side_effect = lambda url, **kwargs: setattr(page, "url", url)
            page.evaluate.return_value = True
            page.locator.return_value = form
            with patch.object(apply, "EXTERNAL", True), patch.object(apply, "EXT_STATE", {"count": 0, "last": 0}), \
                    patch.object(apply, "wait_application_submission", return_value=outcome):
                status = apply.apply_form(page, f"external:{number}", "Engineer", "Example", page.url, self.state)
            self.assertEqual(status, "applied_external" if outcome == "applied" else outcome)
            self.assertEqual(self.state[f"external:{number}"]["status"], status)
            form.click.assert_called_once()


class LoopTests(SafetyTest):
    def test_late_exception_does_not_overwrite_uncertain_state_and_next_run_skips(self):
        state = {}
        job = {"id": "indeed:1", "title": "Engineer", "company": "Example", "desc": "", "url": "https://example.com/job",
               "metadata": {"posted_at": dt.datetime.now(dt.timezone.utc), "workplace": "remote"}}
        def attempt(applied):
            applied[job["id"]] = {"status": "submission_unconfirmed", "key": apply.job_key("Engineer", "Example")}
            raise RuntimeError("late cleanup failure")
        send = MagicMock(side_effect=attempt)
        with patch.object(sources, "SOURCES", {"indeed": lambda page: iter([(job, send)])}), \
                patch.object(apply, "job_ok", return_value=None), patch.object(apply, "record") as record:
            self.assertEqual(sources.run(apply, node(), state, 0), 0)
            self.assertEqual(sources.run(apply, node(), state, 0), 0)
        send.assert_called_once()
        record.assert_not_called()
        self.assertEqual(state[job["id"]]["status"], "submission_unconfirmed")


class SignalTests(SafetyTest):
    def test_stale_or_generic_applied_text_is_not_confirmation(self):
        page = node()
        page.locator.return_value.all_inner_texts.return_value = ["Applied", "Not applied", "Thanks", "Application sent"]
        before = apply.application_submission_signals(page)
        page.locator.return_value.count.return_value = 0
        self.assertEqual(before, {"application sent"})
        self.assertEqual(apply.wait_application_submission(page, before, checks=1), "submission_unconfirmed")

    def test_delayed_explicit_confirmation_is_accepted(self):
        page = node()
        page.locator.return_value.count.return_value = 0
        with patch.object(apply, "application_submission_signals", side_effect=[set(), {"application sent"}]):
            self.assertEqual(apply.wait_application_submission(page, set(), checks=2), "applied")

    def test_untrusted_redirect_does_not_confirm_submission(self):
        page = node()
        page.url = "https://evil.example/thanks"
        self.assertEqual(apply.wait_application_submission(page, set(), checks=1,
                         expected_host="wellfound.com"), "submission_unconfirmed")


@unittest.skipUnless(os.getenv("AUTOAPPLY_BROWSER_TESTS") == "1", "optional local Chrome DOM tests")
class BrowserRetryTests(SafetyTest):
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

    def test_new_confirmation_after_click_is_visible_and_explicit(self):
        self.page.set_content('<main><h1>Thanks</h1><p>Applied</p><div role="status" id="result"></div>'
            '<button onclick="document.getElementById(\'result\').textContent=\'Your application has been submitted\'">Send</button></main>')
        before = apply.application_submission_signals(self.page)
        self.assertEqual(before, set())
        self.page.locator("button").click()
        self.assertEqual(apply.wait_application_submission(self.page, before, checks=1), "applied")

    def test_stale_message_and_validation_error_do_not_trigger_retries(self):
        self.page.set_content('<main><div role="status">Application sent</div></main>')
        before = apply.application_submission_signals(self.page)
        status = apply.wait_application_submission(self.page, before, checks=1)
        self.assertEqual(status, "submission_unconfirmed")
        self.assertFalse(apply.can_retry(status))
        self.page.locator("main").evaluate("e => e.insertAdjacentHTML('beforeend', '<input aria-invalid=\"true\">')")
        status = apply.wait_application_submission(self.page, before, checks=1)
        self.assertEqual(status, "submission_rejected")
        self.assertFalse(apply.can_retry(status))
