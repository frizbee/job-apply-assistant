"""History-backed deferred queue; manual approval never unlocks uncertain attempts."""
import csv
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

import apply
import deferred_review as review
import gui
from test_answer_safety import SafetyTest, node


class QueueTests(SafetyTest):
    def setUp(self):
        super().setUp()
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.manual = self.home / 'manual.csv'
        self.enterContext(patch.object(apply, 'STOP', threading.Event()))
        self.enterContext(patch.object(gui, 'REVIEW_SESSION', None))

    def test_latest_manual_note_is_joined_only_to_current_deferred_history(self):
        with self.manual.open('w', encoding='utf-8', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=['job_id', 'reason', 'apply_url'])
            writer.writeheader()
            writer.writerows([{'job_id': '123', 'reason': 'old'}, {'job_id': '123', 'reason': 'Phone*', 'apply_url': 'https://example.test/form'},
                             {'job_id': 'protected', 'reason': 'old manual note'}])
        history = {'123': {'status': 'manual_review', 'title': 'Engineer', 'company': 'Example', 'url': 'https://www.linkedin.com/jobs/view/123/'},
                   'protected': {'status': 'submission_unconfirmed'}, 'sent': {'status': 'applied'}, 'missing': {'status': 'metadata_manual'}}
        jobs = review.queue(history, self.manual)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]['reason'], 'Phone*')
        self.assertEqual(jobs[0]['job_url'], history['123']['url'])
        self.assertEqual(jobs[0]['apply_url'], 'https://example.test/form')

    def test_authorization_is_scoped_to_selected_job_and_pre_submit_statuses(self):
        self.assertFalse(review.authorized('123', {'status': 'manual_review'}))
        token = review.CURRENT.set(review.Session('123', True))
        self.addCleanup(review.CURRENT.reset, token)
        self.assertTrue(review.authorized('123', {'status': 'manual_review'}))
        self.assertTrue(review.authorized('123', {'status': 'external_manual'}))
        for status in apply.UNCERTAIN_STATUSES | {'applied', 'applied_external', 'submission_rejected', 'arbitrary'}:
            self.assertFalse(review.authorized('123', {'status': status}))
        self.assertFalse(review.authorized('456', {'status': 'manual_review'}))

    def test_stale_or_duplicate_step_commands_are_rejected(self):
        session = review.Session('123', True)
        self.assertIn('error', session.action('continue', 'step'))
        session.state.update(phase='waiting', step='step')
        self.assertIn('error', session.action('continue', 'old'))
        self.assertEqual(session.action('continue', 'step'), {'ok': True})
        self.assertIn('error', session.action('continue', 'step'))

    def test_bulk_manual_review_does_not_wait_for_user(self):
        with patch.object(apply, 'record_manual'), patch.object(review.Session, 'wait') as wait:
            self.assertTrue(apply.require_manual_review(node(), node(), ['Unknown question'], '123', 'Engineer', 'Example'))
        wait.assert_not_called()

    def test_deferred_history_stays_protected_from_automatic_submit(self):
        page, button = node(), node()
        with patch.object(apply, 'DRY_RUN', False):
            self.assertEqual(apply.submit_application(page, button, '123', 'Engineer', 'Example',
                             'https://www.linkedin.com/jobs/view/123/', lambda: 'applied', {'123': {'status': 'manual_review'}}), 'manual_review')
        button.click.assert_not_called()

    def test_manual_session_cannot_send_uncertain_or_confirmed_history(self):
        token = review.CURRENT.set(review.Session('123', False))
        self.addCleanup(review.CURRENT.reset, token)
        with patch.object(apply, 'DRY_RUN', False), patch.object(apply, 'record_manual'):
            for status in ['submission_pending', 'submission_unconfirmed', 'applied']:
                button = node()
                apply.submit_application(node(), button, '123', 'Engineer', 'Example',
                    'https://www.linkedin.com/jobs/view/123/', lambda: 'applied', {'123': {'status': status}})
                button.click.assert_not_called()

    def test_manual_dry_run_never_clicks_or_reserves_submit(self):
        token = review.CURRENT.set(review.Session('123', True))
        self.addCleanup(review.CURRENT.reset, token)
        with patch.object(apply, 'DRY_RUN', True), patch.object(apply, 'preview_application', return_value='dry_run'), patch.object(apply, 'reserve_submission_attempt') as reserve:
            button = node()
            self.assertEqual(apply.submit_application(node(), button, '123', 'Engineer', 'Example',
                             'https://www.linkedin.com/jobs/view/123/', lambda: 'applied', {'123': {'status': 'manual_review'}}), 'dry_run')
        button.click.assert_not_called()
        reserve.assert_not_called()

    def test_reported_external_submission_is_backed_up_and_becomes_uncertain(self):
        self.enterContext(patch.object(gui, 'ACTIVITY_LOCK', threading.Lock()))
        self.enterContext(patch.object(gui, 'STATE', {'running': False, 'analyzing': False}))
        history = {'123': {'status': 'manual_review', 'url': 'https://www.linkedin.com/jobs/view/123/'}}
        with patch.object(apply, 'load_applied', return_value=history), patch.object(apply, 'MANUAL_FILE', self.manual), \
                patch.object(apply.job_rechecks, 'backup') as backup, patch.object(apply, 'record') as record, patch.object(apply, 'record_manual'):
            self.assertEqual(gui.deferred_external({'job_id': '123'}, mark_sent=True), {'ok': True})
        backup.assert_called_once()
        self.assertEqual(record.call_args.args[4], 'submission_unconfirmed')

    def test_review_and_external_actions_cannot_overlap_bulk_worker(self):
        self.enterContext(patch.object(gui, 'ACTIVITY_LOCK', threading.Lock()))
        self.enterContext(patch.object(gui, 'STATE', {'running': True, 'analyzing': False}))
        with patch.object(apply, 'load_applied') as load:
            self.assertEqual(gui.start({}, review_job_id='123')['error'], 'already_running')
            self.assertEqual(gui.deferred_external({'job_id': '123'})['error'], 'already_running')
        load.assert_not_called()

    def test_selected_review_uses_one_job_worker_not_bulk_search(self):
        self.enterContext(patch.object(gui, 'ACTIVITY_LOCK', threading.Lock()))
        self.enterContext(patch.object(gui, 'STATE', {'running': False, 'analyzing': False}))
        resume = self.home / 'resume.pdf'
        resume.write_bytes(b'CV')
        apply.SETTINGS.update(resume=str(resume), cv_hash=hashlib.sha256(b'CV').hexdigest())
        history = {'123': {'status': 'manual_review', 'url': 'https://www.linkedin.com/jobs/view/123/'}}
        with patch.object(apply, 'load_applied', return_value=history), patch.object(apply, 'MANUAL_FILE', self.manual), \
                patch.object(gui, '_prepare_start', return_value={'ok': True}), patch.object(gui.threading, 'Thread') as thread, \
                patch.object(apply.deferred_review, 'run') as run, patch.object(apply, 'run_bot') as bulk:
            self.assertEqual(gui.start({}, review_job_id='123'), {'ok': True})
            self.assertTrue(gui.STATE['running'])
            thread.call_args.kwargs['target']()
            run.assert_called_once()
            self.assertEqual(run.call_args.args[1]['job_id'], '123')
            bulk.assert_not_called()
        self.assertFalse(gui.STATE['running'])
        self.assertFalse(gui.ACTIVITY_LOCK.locked())

    def test_review_does_not_prepare_or_launch_for_protected_history(self):
        self.enterContext(patch.object(gui, 'ACTIVITY_LOCK', threading.Lock()))
        self.enterContext(patch.object(gui, 'STATE', {'running': False, 'analyzing': False}))
        with patch.object(apply, 'load_applied', return_value={'123': {'status': 'submission_pending'}}), \
                patch.object(apply, 'MANUAL_FILE', self.manual), patch.object(gui, '_prepare_start') as prepare:
            self.assertEqual(gui.start({}, review_job_id='123')['error'], 'review_unavailable')
            prepare.assert_not_called()
        self.assertFalse(gui.ACTIVITY_LOCK.locked())


@unittest.skipUnless(os.getenv('AUTOAPPLY_BROWSER_TESTS') == '1', 'local browser assisted-review fixtures')
class BrowserReviewTests(SafetyTest):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.pw = sync_playwright().start()
        cls.addClassCleanup(cls.pw.stop)
        cls.browser = cls.pw.chromium.launch(channel='chrome', headless=True)
        cls.addClassCleanup(cls.browser.close)

    def setUp(self):
        super().setUp()
        self.ctx = self.browser.new_context()
        self.addCleanup(self.ctx.close)
        self.ctx.add_init_script(review.GUARD_JS)
        self.html = '<form><label for="answer">Unknown question*</label><input id="answer" required><button type="submit">Submit</button></form>'
        self.ctx.route('**/*', lambda r: r.fulfill(content_type='text/html', body=self.html))
        self.page = self.ctx.new_page()
        self.page.set_default_timeout(300)
        self.page.goto('https://www.linkedin.com/jobs/view/123/')
        self.form = self.page.locator('form')
        self.enterContext(patch.object(apply, 'STOP', threading.Event()))
        self.enterContext(patch.object(apply, 'DEADLINE', float('inf')))
        self.enterContext(patch.object(apply, 'record_manual'))

    def command_on_tick(self, session, action):
        original = self.page.wait_for_timeout
        def tick(ms):
            action()
            original(1)
        return self.enterContext(patch.object(self.page, 'wait_for_timeout', side_effect=tick))

    def test_user_can_fill_unknown_answer_and_confirm_only_this_step(self):
        session = review.Session('123', True)
        def action():
            self.page.locator('#answer').fill('My reviewed answer')
            session.action('continue', session.public()['step'])
        self.command_on_tick(session, action)
        self.assertTrue(session.wait(apply, self.page, self.form, ['Unknown question*']))
        self.assertEqual(apply.answer('Unknown question*'), [])
        self.assertNotIn('My reviewed answer', str(session.public()))

    def test_empty_required_field_does_not_pass_manual_confirmation(self):
        session = review.Session('123', True)
        calls = []
        def action():
            state = session.public()
            calls.append(state)
            session.action('continue' if len(calls) == 1 else 'skip', state['step'])
        self.command_on_tick(session, action)
        self.assertFalse(session.wait(apply, self.page, self.form, ['Unknown question*']))
        self.assertNotEqual(calls[0]['step'], calls[1]['step'])

    def test_navigation_invalidates_pending_confirmation_even_on_same_host(self):
        session = review.Session('123', True)
        def action():
            self.page.goto('https://www.linkedin.com/jobs/view/456/')
            session.action('continue', session.public()['step'])
        self.command_on_tick(session, action)
        self.assertFalse(session.wait(apply, self.page, self.form, ['Unknown question*']))
        self.assertEqual(self.page.locator('#answer').input_value(), '')

    def test_stop_exits_wait_without_submission(self):
        session = review.Session('123', True)
        self.command_on_tick(session, apply.STOP.set)
        with self.assertRaises(apply.Stopped):
            session.wait(apply, self.page, self.form, ['Unknown question*'])

    def test_browser_submit_is_blocked_until_guarded_application_path(self):
        self.page.evaluate("() => { document.querySelector('button').onclick=()=>{window.clicks=(window.clicks||0)+1}; }")
        self.page.locator('button').click()
        self.assertEqual(self.page.evaluate('window.clicks || 0'), 0)
        self.page.evaluate('window.__autoapplyManualAllowSubmit = true')
        self.page.locator('button').click()
        self.assertEqual(self.page.evaluate('window.clicks'), 1)
        self.page.locator('button').click()
        self.assertEqual(self.page.evaluate('window.clicks'), 1)

    def test_unsafe_action_cannot_be_approved_as_a_question(self):
        session = review.Session('123', True)
        self.assertFalse(session.wait(apply, self.page, self.form, ['submit-capable navigation control']))
        self.assertEqual(session.public()['phase'], 'opening')

    def test_changed_cv_during_review_blocks_continuation(self):
        home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        resume = home / 'resume.pdf'
        resume.write_bytes(b'old CV')
        apply.SETTINGS['resume'] = str(resume)
        session = review.Session('123', True, hashlib.sha256(resume.read_bytes()).hexdigest())
        def action():
            self.page.locator('#answer').fill('Answer')
            resume.write_bytes(b'new CV')
            session.action('continue', session.public()['step'])
        self.command_on_tick(session, action)
        with self.assertRaisesRegex(RuntimeError, 'CV changed'):
            session.wait(apply, self.page, self.form, ['Unknown question*'])

    def test_live_assisted_submit_requires_final_approval_and_durable_pending(self):
        session = review.Session('123', False)
        token = review.CURRENT.set(session)
        self.addCleanup(review.CURRENT.reset, token)
        self.page.locator('#answer').fill('Reviewed answer')
        button = self.page.locator('button')
        history = {'123': {'status': 'manual_review'}}
        statuses = []
        def record(state, job_id, title, company, status, url):
            statuses.append(status)
            state[job_id] = {'status': status}
        original_click = button.click
        def click():
            self.assertEqual(history['123']['status'], 'submission_pending')
            self.page.evaluate("() => { document.querySelector('form').onsubmit=e=>{e.preventDefault();window.sent=true}; }")
            original_click()
        with patch.object(apply, 'DRY_RUN', False), patch.object(session, 'wait', return_value=True) as approval, \
                patch.object(apply, 'record', side_effect=record), patch.object(button, 'click', side_effect=click):
            result = apply.submit_application(self.page, button, '123', 'Engineer', 'Example', self.page.url, lambda: 'applied', history)
        self.assertEqual(result, 'applied')
        approval.assert_called_once()
        self.assertEqual(statuses, ['submission_pending', 'applied'])
        self.assertFalse(self.page.evaluate('window.__autoapplyManualAllowSubmit'))

    def test_deferred_easy_apply_can_reach_preview_after_manual_input_without_sending(self):
        self.html = '''<button type="button" onclick="document.querySelector('dialog').showModal()">Easy Apply</button>
            <dialog><form><label for="answer">Unknown question*</label><input id="answer" required>
            <footer><button type="button" onclick="window.sent=true">Submit application</button></footer></form></dialog>'''
        self.page.goto('https://www.linkedin.com/jobs/view/123/')
        apply.PROFILE['resume_file'] = 'nonexistent-resume.pdf'
        session = review.Session('123', True)
        token = review.CURRENT.set(session)
        self.addCleanup(review.CURRENT.reset, token)
        def action():
            self.page.locator('#answer').fill('Reviewed for this job only')
            session.action('continue', session.public()['step'])
        self.command_on_tick(session, action)
        with patch.object(apply, 'DRY_RUN', True):
            self.assertEqual(apply.apply_to_job(self.page, '123', 'Engineer', 'Example'), 'dry_run')
        self.assertFalse(self.page.evaluate('window.sent === true'))
        self.assertEqual(apply.answer('Unknown question*'), [])

    def test_gui_renders_queue_as_text_and_posts_selected_job_and_step(self):
        import cv_profile
        ctx = self.browser.new_context()
        self.addCleanup(ctx.close)
        page = ctx.new_page()
        profile = cv_profile.empty_profile()
        profile.update(first_name='Test', last_name='User', phone_intl='+90 5551234567', job_titles=['Engineer'])
        posted = []
        def route(r):
            path = r.request.url.split('gui.test')[-1]
            if path == '/':
                return r.fulfill(content_type='text/html', body=gui.PAGE.replace('__TOKEN__', 'fixture'))
            if path == '/api/state':
                data = {'settings': {'test_mode': True}, 'profile': profile, 'default_locations': ['User region'], 'cv_hash': 'cv'}
            elif path == '/api/deferred':
                data = {'jobs': [{'job_id': '123', 'title': '<img src=x onerror=alert(1)>', 'company': 'Example', 'reason': 'Unknown question', 'can_assist': True}]}
            elif path.startswith('/api/log'):
                data = {'lines': [], 'running': True, 'review': {'job_id': '123', 'phase': 'waiting', 'step': 'step1', 'issues': ['Unknown question'], 'test_mode': True}}
            else:
                posted.append((path, r.request.post_data_json))
                data = {'ok': True}
            r.fulfill(content_type='application/json', body=json.dumps(data))
        ctx.route('**/*', route)
        page.goto('http://gui.test/')
        page.get_by_role('button', name='Review this application', exact=True).click()
        page.locator('#review_continue').wait_for()
        page.wait_for_function('document.getElementById("review_continue").disabled === false')
        page.locator('#review_continue').click()
        page.wait_for_timeout(50)
        self.assertEqual(posted[0][0], '/api/review/start')
        self.assertEqual(posted[0][1]['job_id'], '123')
        self.assertIs(posted[0][1]['test_mode'], True)
        self.assertEqual(posted[-1], ('/api/review/action', {'action': 'continue', 'step': 'step1'}))
        self.assertEqual(page.locator('#deferred_jobs img').count(), 0)
