"""Offline attention checks must defer, never generate or submit an answer."""
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch, MagicMock

import apply
import deferred_review
import description_instructions as instructions


class DetectionTests(unittest.TestCase):
    def test_many_employer_formulations(self):
        examples = [
            'Include the phrase "purple elephant" in your cover letter.',
            'Please mention the word pineapple in your application.',
            'Write the keyword banana in your response.',
            'Type the code word sunshine in your answer.',
            'Insert the password "Rails rocks" into your message.',
            'Add the sentence "I read everything" to your covering letter.',
            'Put the secret code BLUE in your application.',
            'Use the token ORANGE in your reply.',
            'Enter the string "ruby-2026" in your response.',
            'State the phrase below in your letter of motivation:\nBlue sky.',
            'Copy the following text into your cover-letter: GREEN.',
            'Paste the statement below in your introductory note.',
            'Your application must include the word giraffe.',
            'Your cover letter should mention this phrase: blue moon.',
            'Your answer must contain "red balloon".',
            'Responses containing the keyword sunrise will be considered.',
            'Include "blue sky" in your cover letter.',
            'Mention “Ruby Tuesday” in your application.',
            'Please write ‘hello there’ in your email.',
            'Start your cover letter with banana.',
            'Begin your application with the phrase below:\nRuby forever.',
            'End your response with thank you.',
            'Finish your cover letter with the word done.',
            'Open your message with hello team.',
            'Close your email with purple cow.',
            'Your cover letter must start with hello.',
            'Your response should end with goodbye.',
            'Put BANANA at the top of your cover letter.',
            'Add BLUE as the first word of your application.',
            'Subject line: Rails Engineer - banana',
            'The email subject must be Ruby application.',
            'To prove you read this description, include BANANA.',
            'To demonstrate attention to detail, write blue sky.',
            'So we know you are not a bot, mention pineapple.',
            'Include the word kiwi to show you read this posting.',
            'INCLUDE\nTHE PHRASE\n"Exact-CASE!"\nIN YOUR COVER LETTER.',
        ]
        for text in examples:
            with self.subTest(text=text):
                self.assertTrue(instructions.find(text))

    def test_normal_description_is_not_an_attention_check(self):
        for text in ['', 'We use Ruby on Rails and write clean code.',
                     'Please include your CV and cover letter.',
                     'Mention your salary expectations in your application.',
                     'Write a cover letter describing your experience.',
                     'Applications open on Monday and close on Friday.',
                     'Keywords: Ruby, Rails. We build applications.',
                     'We use the phrase continuous improvement internally.',
                     'The application contains a text editor.',
                     'You will start with our application team.',
                     'Please describe why you want to join us.']:
            with self.subTest(text=text):
                self.assertEqual(instructions.find(text), [])

    def test_next_line_literal_and_exact_case_remain_visible(self):
        text = 'Include the phrase below in your cover letter:\n\nRuby-ROCKS_2026!'
        found = instructions.find(text)
        self.assertEqual(len(found), 1)
        self.assertIn('Ruby-ROCKS_2026!', found[0])


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        for name in ['APPLIED_FILE', 'LOG_FILE', 'MANUAL_FILE']:
            self.enterContext(patch.object(apply, name, self.home / name))
        self.enterContext(patch.object(apply, 'log'))
        self.job = {'id': 'phrase-test', 'title': 'Ruby Engineer', 'company': 'Example',
                    'url': 'https://example.test/jobs/1',
                    'desc': 'Include the phrase "Ruby forever" in your cover letter.'}

    def test_shared_gate_defers_and_persists_queue_evidence(self):
        history = {}
        with patch.object(apply.job_metadata, 'eligibility_reason', return_value=None):
            self.assertFalse(apply.check_job_eligibility(self.job, history))
        entry = history[self.job['id']]
        self.assertEqual(entry['status'], 'manual_review')
        self.assertIn('Ruby forever', entry['description_instructions'][0])
        queue = deferred_review.queue(history, apply.MANUAL_FILE)
        self.assertEqual(len(queue), 1)
        self.assertIn('Ruby forever', queue[0]['reason'])
        # A normal subsequent job remains eligible: no bulk-run interruption.
        with patch.object(apply.job_metadata, 'eligibility_reason', return_value=None):
            self.assertTrue(apply.check_job_eligibility({**self.job, 'id': 'next', 'desc': 'Build Ruby services.'}, history))

    def test_metadata_description_fallback(self):
        job = {k: v for k, v in self.job.items() if k != 'desc'}
        job['metadata'] = {'description': self.job['desc']}
        self.assertFalse(apply.check_description_instructions(job, {}))

    def test_manual_session_keeps_requirement_for_form_approval(self):
        session = deferred_review.Session(self.job['id'], True)
        token = deferred_review.CURRENT.set(session)
        self.addCleanup(deferred_review.CURRENT.reset, token)
        with patch.object(apply, 'record') as record:
            self.assertTrue(apply.check_description_instructions(self.job, {}))
            record.assert_not_called()
        self.assertIn('Ruby forever', session.description_instructions[0])
        form = MagicMock()
        form.locator.return_value.all.return_value = []
        with patch.object(apply, 'unconfirmed_text_questions', return_value=[]), \
             patch.object(apply, 'unconfirmed_radio_questions', return_value=[]), \
             patch.object(apply, 'unconfirmed_checkbox_questions', return_value=[]), \
             patch.object(apply, 'native_form_issues', return_value=[]), \
             patch.object(apply, 'screenshot'), patch.object(session, 'wait', return_value=False) as wait:
            self.assertTrue(apply.require_manual_review(MagicMock(), form, [], self.job['id'], 'Engineer', 'Example'))
        self.assertIn('Ruby forever', wait.call_args.args[-1][0])

    def test_protected_history_never_rewritten(self):
        for status in apply.UNCERTAIN_STATUSES | {'applied', 'applied_external', 'submission_rejected'}:
            with self.subTest(status=status), patch.object(apply, 'record') as record:
                history = {self.job['id']: {'status': status}}
                self.assertFalse(apply.check_description_instructions(self.job, history))
                self.assertEqual(history[self.job['id']]['status'], status)
                record.assert_not_called()


if __name__ == '__main__':
    unittest.main()
