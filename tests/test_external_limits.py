"""Per-run external quotas with a process-wide inter-attempt cooldown."""
import threading
from unittest.mock import MagicMock, call, patch

from test_answer_safety import SafetyTest, node
import apply
import sources


class ExternalLimitTests(SafetyTest):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.object(apply, "EXT_STATE", {"count": 5, "last": 1000.0}))
        self.enterContext(patch.object(apply, "STOP", threading.Event()))
        self.enterContext(patch.object(apply, "DEADLINE", 0.0))
        self.enterContext(patch.object(apply, "EXTERNAL", True))
        self.enterContext(patch.object(apply.time, "time", return_value=1000.0))
        self.enterContext(patch.object(apply, "load_applied", return_value={}))
        self.enterContext(patch.object(apply, "LAST_RUN_FILE", MagicMock()))
        self.enterContext(patch.object(apply, "sync_playwright"))
        self.ctx = node()
        self.page = node()
        self.ctx.pages = [self.page]
        self.launch = self.enterContext(patch.object(apply, "launch", return_value=self.ctx))
        self.enterContext(patch.object(apply, "ats_apply_url",
            return_value=("https://jobs.lever.co/example/job/apply", "lever")))
        self.form = self.enterContext(patch.object(apply, "apply_trusted_form", return_value="dry_run"))
        apply.CFG["sources"]["linkedin"] = False
        apply.CFG["external"].update(max_per_run=2, min_wait_seconds=180)

    def attempt(self):
        return apply.apply_form(self.page, "job", "Engineer", "Example", "https://jobs.lever.co/example/job")

    def one_attempt(self, *args):
        self.attempt()
        return 0

    def test_two_runs_each_receive_full_quota_and_enforce_it_within_run(self):
        results = []
        def work(*args):
            results.append([self.attempt() for _ in range(3)])
            return 0
        with patch.object(sources, "run", side_effect=work):
            self.assertEqual(apply.run_bot(), 0)
            self.assertEqual(apply.run_bot(), 0)
        self.assertEqual(results, [["dry_run", "dry_run", "skipped_external_limit"]] * 2)
        self.assertEqual(self.form.call_count, 4)
        self.assertEqual(apply.EXT_STATE["count"], 2)

    def test_restart_resets_count_but_keeps_remaining_cooldown(self):
        def work(*args):
            self.assertEqual(apply.EXT_STATE, {"count": 0, "last": 1000.0})
            self.assertEqual(self.attempt(), "dry_run")
            return 0
        with patch.object(sources, "run", side_effect=work):
            apply.run_bot()
        self.assertEqual(apply.pause.call_args_list[0], call(180.0, 180.0))

    def test_elapsed_cooldown_does_not_add_an_unnecessary_wait(self):
        apply.EXT_STATE["last"] = 500.0
        with patch.object(sources, "run", side_effect=self.one_attempt):
            apply.run_bot()
        self.assertEqual(apply.pause.call_args_list, [call(3, 5)])

    def test_stop_then_restart_clears_stop_and_restores_quota(self):
        def stop(*args):
            self.attempt()
            apply.STOP.set()
            raise apply.Stopped()
        def restart(*args):
            self.assertFalse(apply.STOP.is_set())
            self.assertEqual(apply.EXT_STATE["count"], 0)
            self.assertEqual(apply.EXT_STATE["last"], 1000.0)
            self.assertEqual(self.attempt(), "dry_run")
            return 0
        with patch.object(sources, "run", side_effect=stop):
            apply.run_bot()
        with patch.object(sources, "run", side_effect=restart):
            apply.run_bot()
        self.assertEqual(self.form.call_count, 2)
        self.assertEqual(self.ctx.close.call_count, 2)

    def test_failed_browser_launch_does_not_poison_next_run_quota(self):
        self.launch.side_effect = [RuntimeError("Chrome unavailable"), self.ctx]
        with self.assertRaises(RuntimeError):
            apply.run_bot()
        self.assertEqual(apply.EXT_STATE, {"count": 0, "last": 1000.0})
        with patch.object(sources, "run", side_effect=self.one_attempt):
            self.assertEqual(apply.run_bot(), 0)
        self.assertEqual(self.form.call_count, 1)

    def test_aborted_run_does_not_leave_consumed_quota_in_next_run(self):
        def abort(*args):
            self.attempt()
            raise RuntimeError("source failed")
        with patch.object(sources, "run", side_effect=abort):
            with self.assertRaises(RuntimeError):
                apply.run_bot()
        self.assertEqual(apply.EXT_STATE["count"], 1)
        with patch.object(sources, "run", side_effect=self.one_attempt):
            self.assertEqual(apply.run_bot(), 0)
        self.assertEqual(apply.EXT_STATE["count"], 1)
        self.assertEqual(self.form.call_count, 2)

    def test_zero_quota_remains_disabled_after_restart(self):
        apply.CFG["external"]["max_per_run"] = 0
        def work(*args):
            self.assertEqual(self.attempt(), "skipped_external_limit")
            return 0
        with patch.object(sources, "run", side_effect=work):
            apply.run_bot()
            apply.run_bot()
        self.form.assert_not_called()
