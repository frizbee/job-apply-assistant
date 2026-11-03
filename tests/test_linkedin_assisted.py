import unittest
import hashlib
from pathlib import Path
import tempfile
from unittest.mock import MagicMock, patch

from tools.linkedin_assisted import Assisted, TITLES


class ApprovalTests(unittest.TestCase):
    def test_nine_exact_search_titles(self):
        self.assertEqual(len(TITLES), 9)
        self.assertEqual(TITLES[0], 'Senior Ruby on Rails Developer')
        self.assertEqual(TITLES[-1], 'Senior Full Stack Developer')

    def test_requires_application_step_and_explicit_consent(self):
        session = Assisted.__new__(Assisted)
        session.current = {'job_id': '123', 'step': 'one'}
        for body in [{}, {'job_id': '124', 'step': 'one', 'consent': True},
                     {'job_id': '123', 'step': 'old', 'consent': True},
                     {'job_id': '123', 'step': 'one', 'consent': False}]:
            with self.subTest(body=body), patch('apply.submit_linkedin') as submit:
                self.assertIn('error', session.ready(body))
                submit.assert_not_called()

    def test_search_does_not_leave_current_application(self):
        session = Assisted.__new__(Assisted)
        session.current = {'job_id': '123'}
        session.status = MagicMock(return_value={})
        self.assertIn('error', session.search_next())

    def test_no_easy_apply_opens_unknown_external_form_instead_of_rejecting(self):
        session = Assisted.__new__(Assisted)
        session.page = MagicMock()
        session.page.url = 'https://careers.example.org/application/123'
        session.page.get_by_role.return_value.first.count.return_value = 0
        session.page.title.return_value = 'Employer application'
        session.page.locator.return_value.all_inner_texts.return_value = []
        session.page.locator.return_value.inner_text.return_value = 'Application fields'
        session.status = MagicMock(return_value={})
        row = dict(job_id='123', title='Engineer', company='Example', url='https://www.linkedin.com/jobs/view/123/')
        with patch('apply.external_url', return_value=session.page.url), \
                patch('apply.load_applied', return_value={}), patch('apply.mark_evaluation_passed'), \
                patch('candidate_workflow.store') as store, patch('apply.submit_application') as submit:
            result = session.open_form(row)
            self.assertTrue(result['waiting_for_user'])
            self.assertEqual(session.current['kind'], 'external_manual')
            store.return_value.put.assert_called_once()
            submit.assert_not_called()

    def test_unknown_external_site_ready_does_not_automatically_submit(self):
        with tempfile.TemporaryDirectory() as folder:
            session = Assisted.__new__(Assisted)
            session.cv = Path(folder) / 'cv.pdf'
            session.cv.write_bytes(b'cv')
            session.cv_hash = hashlib.sha256(b'cv').hexdigest()
            session.current = dict(job_id='123', step='one', kind='external_manual',
                                   document_url='https://careers.example.org/application/123')
            session.page = MagicMock(url=session.current['document_url'])
            session.document = MagicMock()
            session.document.evaluate.return_value = True
            session.status = MagicMock(return_value={})
            with patch('apply.submit_application') as submit:
                result = session.ready(dict(job_id='123', step='one', consent=True))
                self.assertTrue(result['manual_submission_required'])
                submit.assert_not_called()
                session.page.evaluate.assert_not_called()

    def test_manual_sending_is_forbidden_in_test_mode(self):
        session = Assisted.__new__(Assisted)
        session.current = dict(job_id='123', step='one', kind='external_manual')
        with patch('apply.DRY_RUN', True), patch('apply.reserve_submission_attempt') as reserve:
            self.assertIn('error', session.manual_send(dict(job_id='123', step='one', consent=True)))
            reserve.assert_not_called()


if __name__ == '__main__':
    unittest.main()
