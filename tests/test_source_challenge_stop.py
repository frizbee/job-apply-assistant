"""A failed detail verification must not navigate through every remaining job."""
import datetime as dt
import unittest
from unittest.mock import MagicMock, patch

import apply
import sources
from test_answer_safety import SafetyTest, node


class ChallengeRecognitionTests(SafetyTest):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.object(sources, 'core', apply))

    def test_cloudflare_verifying_variants_are_blocked(self):
        for text in ('Verifying you are human. This may take a few seconds.',
                     'This page is displayed while the website verifies you are not a bot.',
                     'Verify you are human', 'Additional Verification Required'):
            with self.subTest(text=text):
                page = node()
                page.title.return_value = 'Himalayas'
                page.locator.return_value.inner_text.return_value = text
                self.assertTrue(apply.blocked(page))
                self.assertFalse(sources.page_ready(page))

    def test_normal_job_text_does_not_match_new_challenge_phrases(self):
        page = node()
        page.title.return_value = 'Senior Backend Engineer'
        page.locator.return_value.inner_text.return_value = 'We build remote software and verification services for customers.'
        self.assertFalse(apply.blocked(page))


class SourceChallengeStopTests(SafetyTest):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.object(sources, 'core', apply))
        self.enterContext(patch.object(apply, 'DEADLINE', float('inf')))
        self.enterContext(patch.object(apply, 'job_ok', return_value=None))
        self.record = self.enterContext(patch.object(apply, 'record'))
        self.enterContext(patch.object(apply, 'record_manual'))
        apply.CFG['sources'].update(himalayas=True, weworkremotely=True)
        self.page = node()
        self.page.url = 'https://himalayas.app/jobs'
        self.page.title.return_value = 'Himalayas'
        self.page.locator.return_value.inner_text.return_value = ''
        self.first = self.job('first')
        self.second = self.job('second')

    def job(self, name, remote=False):
        return {'id': 'himalayas:' + name, 'title': 'Senior Backend Engineer', 'company': 'Example',
                'url': 'https://himalayas.app/companies/example/jobs/' + name,
                'desc': 'Full employer description.', 'metadata': {
                    'posted_at': dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1),
                    'workplace': 'remote' if remote else None}}

    def test_failed_metadata_navigation_stops_after_first_job(self):
        first_send, second_send = MagicMock(), MagicMock()
        with patch.object(sources, 'SOURCES', {'himalayas': lambda _p: iter([(self.first, first_send), (self.second, second_send)])}), \
             patch.object(sources, 'goto', return_value=False) as goto, \
             patch.object(apply, 'read_job_metadata') as metadata:
            self.assertEqual(sources.run(apply, self.page, {}, 0), 0)
        goto.assert_called_once_with(self.page, self.first['url'], 'himalayas')
        metadata.assert_not_called()
        first_send.assert_not_called()
        second_send.assert_not_called()
        self.assertEqual(apply.METRICS.snapshot()['evaluated'], 1)
        self.assertEqual(self.record.call_args.args[4], 'metadata_manual')

    def test_already_open_challenge_is_not_parsed_or_replaced_by_next_job(self):
        self.page.url = self.first['url']
        self.page.locator.return_value.inner_text.return_value = 'Verifying you are human. This may take a few seconds.'
        with patch.object(sources, 'SOURCES', {'himalayas': lambda _p: iter([(self.first, MagicMock()), (self.second, MagicMock())])}), \
             patch.object(sources, 'goto') as goto, patch.object(apply, 'read_job_metadata') as metadata:
            self.assertEqual(sources.run(apply, self.page, {}, 0), 0)
        goto.assert_not_called()
        metadata.assert_not_called()
        self.assertEqual(apply.METRICS.snapshot()['evaluated'], 1)

    def test_other_sources_continue_after_blocked_source_stops(self):
        other = self.job('other', remote=True)
        other['id'] = 'wwr:other'
        send = MagicMock(return_value='dry_run')
        with patch.object(sources, 'SOURCES', {
                'himalayas': lambda _p: iter([(self.first, MagicMock()), (self.second, MagicMock())]),
                'weworkremotely': lambda _p: iter([(other, send)])}), \
             patch.object(sources, 'goto', return_value=False) as goto:
            self.assertEqual(sources.run(apply, self.page, {}, 0), 1)
        goto.assert_called_once()
        send.assert_called_once()

    def test_successful_manual_verification_still_resolves_metadata(self):
        send = MagicMock(return_value='dry_run')
        with patch.object(sources, 'SOURCES', {'himalayas': lambda _p: iter([(self.first, send)])}), \
             patch.object(sources, 'goto', return_value=True) as goto, \
             patch.object(apply, 'read_job_metadata', return_value=self.job('resolved', remote=True)['metadata']) as metadata:
            self.assertEqual(sources.run(apply, self.page, {}, 0), 1)
        goto.assert_called_once()
        metadata.assert_called_once_with(self.page, 'himalayas', self.first['url'])
        send.assert_called_once()


if __name__ == '__main__':
    unittest.main()
