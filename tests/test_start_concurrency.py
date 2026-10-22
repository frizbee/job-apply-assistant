"""Concurrent GUI requests cannot change an accepted run or create extra workers."""
import base64
import copy
import hashlib
import json
from pathlib import Path
from queue import Queue
import tempfile
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from test_answer_safety import SafetyTest
import apply
import cv_profile
import gui

REAL_THREAD = threading.Thread


class StartConcurrencyTests(SafetyTest):
    def setUp(self):
        super().setUp()
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="start-concurrency-")))
        (self.home / "cv").mkdir()
        self.resume = self.home / "cv" / "resume.pdf"
        self.resume.write_bytes(b"original resume")
        apply.SETTINGS.update(resume=str(self.resume), notice="14")
        self.enterContext(patch.object(apply, "HOME", self.home))
        self.enterContext(patch.object(apply, "DRY_RUN", False))
        self.enterContext(patch.object(apply, "STOP", threading.Event()))
        self.enterContext(patch.object(apply, "LOG_LINES", ["previous log"]))
        self.enterContext(patch.object(apply, "SETTINGS_FILE", self.home / "settings.json"))
        self.enterContext(patch.object(gui, "STATE", {"running": False, "analyzing": False}))
        self.enterContext(patch.object(gui, "ACTIVITY_LOCK", threading.Lock(), create=True))
        self.enterContext(patch.object(apply, "find_key", return_value="test-key"))
        self.real_finalize = apply.finalize_settings
        self.finalize = self.enterContext(patch.object(apply, "finalize_settings"))
        self.workers = []
        self.next_start_error = None
        self.factory = self.enterContext(patch.object(gui.threading, "Thread", side_effect=self.make_worker))

    def make_worker(self, *, target, daemon):
        start = MagicMock(side_effect=self.next_start_error)
        self.next_start_error = None
        worker = SimpleNamespace(target=target, start=start)
        self.workers.append(worker)
        return worker

    def body(self, test_mode=True):
        prof = cv_profile.empty_profile()
        prof.update(first_name="Test", last_name="User", email="test@example.com", phone_intl="+1 555 123 4567",
                    city="Toronto", country="Canada", total_years=3, job_titles=["Software Engineer"])
        return {"profile": prof, "cv_hash": hashlib.sha256(self.resume.read_bytes()).hexdigest(),
                "english": "3", "notice": "14", "locations": ["Worldwide"],
                "sources": ["linkedin"] if test_mode else ["indeed"], "test_mode": test_mode,
                "currency": "USD", "salary": "5000", "salary_eur": "4500", "salary_usd": "5000"}

    def finish_worker(self, index=-1, error=None):
        with patch.object(apply, "run_bot", return_value=0, side_effect=error):
            self.workers[index].target()

    def snapshot(self):
        return copy.deepcopy((apply.SETTINGS, apply.CFG, apply.PROFILE, apply.DRY_RUN, apply.LOG_LINES))

    def assert_idle(self):
        self.assertFalse(gui.STATE["running"])
        self.assertFalse(gui.STATE["analyzing"])
        self.assertFalse(gui.ACTIVITY_LOCK.locked())

    def background(self, target):
        thread = REAL_THREAD(target=target, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        return thread

    def test_second_start_before_worker_scheduling_is_rejected_without_mutation(self):
        self.assertEqual(gui.start(self.body(True)), {"ok": True})
        accepted = self.snapshot()
        self.assertEqual(gui.start(self.body(False)), {"error": "already_running"})
        self.assertEqual(len(self.workers), 1)
        self.assertEqual(self.snapshot(), accepted)
        self.assertTrue(gui.STATE["running"])
        self.assertTrue(apply.DRY_RUN)
        self.finalize.assert_called_once()

    def test_rejected_start_does_not_rewrite_saved_preferences(self):
        self.finalize.side_effect = self.real_finalize
        self.assertEqual(gui.start(self.body(True)), {"ok": True})
        before = apply.SETTINGS_FILE.read_bytes()
        self.assertEqual(gui.start(self.body(False)), {"error": "already_running"})
        self.assertEqual(apply.SETTINGS_FILE.read_bytes(), before)
        self.finalize.assert_called_once()

    def test_simultaneous_requests_create_exactly_one_worker(self):
        barrier, results = threading.Barrier(3), Queue()
        def request(mode):
            barrier.wait(timeout=5)
            results.put((mode, gui.start(self.body(mode))))
        first = self.background(lambda: request(True))
        second = self.background(lambda: request(False))
        barrier.wait(timeout=5)
        first.join(5)
        second.join(5)
        self.assertFalse(first.is_alive() or second.is_alive())
        responses = [results.get_nowait(), results.get_nowait()]
        accepted = [(mode, r) for mode, r in responses if r.get("ok")]
        rejected = [r for _, r in responses if r.get("error")]
        self.assertEqual(len(accepted), 1, responses)
        self.assertEqual(len(rejected), 1, responses)
        self.assertIn(rejected[0]["error"], ("already_running", "busy"))
        self.assertEqual(len(self.workers), 1)
        self.assertIs(apply.DRY_RUN, accepted[0][0])
        self.assertEqual(apply.SETTINGS["sources"], self.body(accepted[0][0])["sources"])

    def test_request_during_preparation_is_rejected_before_worker_exists(self):
        entered, release, results = threading.Event(), threading.Event(), Queue()
        self.addCleanup(release.set)
        def prepare():
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test preparation timed out")
        self.finalize.side_effect = prepare
        thread = self.background(lambda: results.put(gui.start(self.body(True))))
        try:
            self.assertTrue(entered.wait(5))
            accepted = self.snapshot()
            self.assertEqual(gui.start(self.body(False)), {"error": "already_running"})
            self.assertEqual(self.snapshot(), accepted)
            self.assertEqual(self.workers, [])
        finally:
            release.set()
            thread.join(5)
        self.assertEqual(results.get_nowait(), {"ok": True})
        self.assertEqual(len(self.workers), 1)

    def test_invalid_request_releases_reservation_and_can_be_corrected(self):
        before = self.snapshot()
        bad = self.body()
        bad["profile"]["total_years"] = -1
        self.assertEqual(gui.start(bad)["error"], "invalid_profile")
        self.assert_idle()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(gui.start(self.body()), {"ok": True})

    def test_preparation_exception_restores_memory_and_releases_reservation(self):
        before = self.snapshot()
        self.finalize.side_effect = OSError("settings write failed")
        self.assertEqual(gui.start(self.body())["error"], "startup_failed")
        self.assert_idle()
        self.assertEqual(self.snapshot(), before)
        self.assertIs(apply.PROFILE, apply.CFG["profile"])
        self.factory.assert_not_called()
        self.finalize.side_effect = None
        self.assertEqual(gui.start(self.body()), {"ok": True})

    def test_thread_constructor_failure_releases_reservation(self):
        before = self.snapshot()
        self.factory.side_effect = RuntimeError("cannot create worker")
        self.assertEqual(gui.start(self.body())["error"], "startup_failed")
        self.assert_idle()
        self.assertEqual(self.snapshot(), before)
        self.factory.side_effect = self.make_worker
        self.assertEqual(gui.start(self.body()), {"ok": True})

    def test_thread_start_failure_releases_reservation(self):
        before = self.snapshot()
        self.next_start_error = RuntimeError("cannot start worker")
        self.assertEqual(gui.start(self.body())["error"], "startup_failed")
        self.assert_idle()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(gui.start(self.body()), {"ok": True})

    def test_worker_success_releases_reservation_for_next_run(self):
        gui.start(self.body(True))
        self.finish_worker()
        self.assert_idle()
        self.assertEqual(gui.start(self.body(False)), {"ok": True})
        self.assertFalse(apply.DRY_RUN)

    def test_actual_running_worker_keeps_requests_blocked_until_completion(self):
        entered, release, threads = threading.Event(), threading.Event(), []
        def make_thread(**kwargs):
            thread = REAL_THREAD(**kwargs)
            threads.append(thread)
            return thread
        def run(**kwargs):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test worker timed out")
            return 0
        self.factory.side_effect = make_thread
        with patch.object(apply, "run_bot", side_effect=run):
            try:
                self.assertEqual(gui.start(self.body(True)), {"ok": True})
                self.assertTrue(entered.wait(5))
                accepted = self.snapshot()
                self.assertTrue(gui.state()["running"])
                self.assertEqual(gui.start(self.body(False)), {"error": "already_running"})
                self.assertEqual(self.snapshot(), accepted)
                self.assertEqual(len(threads), 1)
            finally:
                release.set()
                for thread in threads:
                    thread.join(5)
        self.assert_idle()

    def test_worker_finishing_inside_thread_start_does_not_double_release(self):
        self.factory.side_effect = lambda target, daemon: SimpleNamespace(start=target)
        with patch.object(apply, "run_bot", return_value=0):
            self.assertEqual(gui.start(self.body()), {"ok": True})
            self.assert_idle()
            self.assertEqual(gui.start(self.body()), {"ok": True})
            self.assert_idle()

    def test_stop_before_worker_starts_is_not_cleared_by_worker(self):
        gui.start(self.body(True))
        apply.STOP.set()
        self.assertEqual(gui.start(self.body(False)), {"error": "already_running"})
        self.assertTrue(apply.STOP.is_set())
        with patch.object(apply, "sync_playwright") as browser, patch.object(apply, "load_applied") as history:
            self.workers[-1].target()
        browser.assert_not_called()
        history.assert_not_called()
        self.assert_idle()
        self.assertEqual(gui.start(self.body(False)), {"ok": True})
        self.assertFalse(apply.STOP.is_set())

    def test_worker_failure_or_stop_always_releases_reservation(self):
        for error in [RuntimeError("browser failed"), apply.Stopped(), KeyboardInterrupt()]:
            self.assertEqual(gui.start(self.body()), {"ok": True})
            self.finish_worker(error=error)
            self.assert_idle()

    def test_upload_and_analysis_cannot_change_active_run(self):
        gui.start(self.body(True))
        accepted = self.snapshot()
        with patch.object(cv_profile, "extract") as extract:
            self.assertEqual(gui.analyze({"key": ""}), {"error": "already_running"})
        extract.assert_not_called()
        result = gui.save_cv({"name": "resume.pdf", "data": base64.b64encode(b"new resume").decode()})
        self.assertEqual(result, {"error": "already_running"})
        self.assertEqual(self.resume.read_bytes(), b"original resume")
        self.assertEqual(self.snapshot(), accepted)

    def test_start_and_upload_wait_for_analysis_to_complete(self):
        entered, release, results = threading.Event(), threading.Event(), Queue()
        self.addCleanup(release.set)
        def extract(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test analysis timed out")
            return self.body()["profile"]
        with patch.object(cv_profile, "extract", side_effect=extract):
            thread = self.background(lambda: results.put(gui.analyze({"key": ""})))
            try:
                self.assertTrue(entered.wait(5))
                before = self.snapshot()
                self.assertEqual(gui.start(self.body()), {"error": "busy"})
                self.assertEqual(gui.save_cv({"name": "resume.pdf", "data": ""}), {"error": "busy"})
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(self.resume.read_bytes(), b"original resume")
            finally:
                release.set()
                thread.join(5)
        self.assertTrue(results.get_nowait().get("ok"))
        self.assert_idle()
        self.assertEqual(gui.start(self.body()), {"ok": True})

    def test_analysis_validation_error_does_not_leave_lock_held(self):
        self.assertEqual(gui.analyze({"key": "invalid"}), {"error": "short_key"})
        self.assert_idle()
        self.assertEqual(gui.start(self.body()), {"ok": True})

    def test_upload_validation_error_does_not_leave_lock_held(self):
        self.assertEqual(gui.save_cv({"name": "resume.txt", "data": ""}), {"error": "pdf_docx_only"})
        self.assert_idle()
        self.assertEqual(gui.start(self.body()), {"ok": True})

    def test_upload_io_failure_does_not_leave_lock_held(self):
        with patch.object(Path, "write_bytes", side_effect=OSError("upload write failed")):
            with self.assertRaises(OSError):
                gui.save_cv({"name": "resume.pdf", "data": base64.b64encode(b"new resume").decode()})
        self.assert_idle()
        self.assertEqual(self.resume.read_bytes(), b"original resume")
        self.assertEqual(gui.start(self.body()), {"ok": True})

    def test_mode_and_source_settings_are_validated_before_publication(self):
        for field, bad in [("test_mode", "false"), ("english", "unknown"), ("sources", "linkedin"), ("locations", "Worldwide"), ("locations", [])]:
            before = self.snapshot()
            body = self.body()
            body[field] = bad
            self.assertEqual(gui.start(body)["error"], "invalid_settings")
            self.assert_idle()
            self.assertEqual(self.snapshot(), before)
        self.factory.assert_not_called()

    def test_technology_settings_are_saved_and_applied_without_invented_defaults(self):
        body = self.body()
        body.update(target_technologies=[' ArbitraryStack ', 'C++'], excluded_technologies=['OtherStack'], excluded_title_words=[' Marketing '])
        self.finalize.side_effect = self.real_finalize
        self.assertEqual(gui.start(body), {'ok': True})
        saved = json.loads(apply.SETTINGS_FILE.read_text(encoding='utf-8'))
        self.assertEqual(saved['target_technologies'], ['ArbitraryStack', 'C++'])
        self.assertEqual(saved['excluded_technologies'], ['OtherStack'])
        self.assertEqual(saved['excluded_title_words'], ['Marketing'])
        self.assertIsNone(apply.job_ok('Software Engineer', 'Example', 'ArbitraryStack with Python required.'))
        self.assertEqual(apply.job_ok('Software Engineer', 'Example', 'OtherStack required.'), 'excluded_technology')
        self.finish_worker()
        body.update(target_technologies=[], excluded_technologies=[], excluded_title_words=[])
        self.assertEqual(gui.start(body), {'ok': True})
        self.assertEqual(apply.SETTINGS['excluded_title_words'], [])
        self.assertIsNone(apply.job_ok('Software Engineer', 'Example', 'OtherStack required.'))

    def test_bad_or_conflicting_technology_rules_do_not_publish_or_start(self):
        for rules in [{'excluded_technologies': 'Python'}, {'target_technologies': [1]},
                      {'excluded_technologies': ['x\ny']}, {'excluded_title_words': 'Marketing'},
                      {'excluded_title_words': ['']},
                      {'target_technologies': ['Ruby'], 'excluded_technologies': ['ruby']}]:
            before = self.snapshot()
            body = {**self.body(), **rules}
            self.assertEqual(gui.start(body)['error'], 'invalid_settings')
            self.assertEqual(self.snapshot(), before)
            self.assert_idle()
        self.factory.assert_not_called()
