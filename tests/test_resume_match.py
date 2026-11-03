import copy
import json
import unittest
from unittest.mock import patch

import resume_match

JD = 'Own production systems. Lead the team. Build payments. Design APIs. React preferred.'
CV = 'Owned production systems. Led a team of five. Built payments. Designed APIs.'


def criterion(requirement, jd_quote, cv_quote, importance='core', verdict='meets'):
    return dict(requirement=requirement, jd_quote=jd_quote, cv_quote=cv_quote,
                importance=importance, verdict=verdict, explanation='Compared the quoted scope and evidence.')


def review():
    return {'complete': True, 'criteria': [
        criterion('Ownership', 'Own production systems.', 'Owned production systems.', 'mandatory'),
        criterion('Leadership', 'Lead the team.', '', 'mandatory', 'not_evidenced'),
        criterion('Payments', 'Build payments.', 'Built payments.'),
        criterion('APIs', 'Design APIs.', 'Designed APIs.'),
    ], 'blockers': []}


class EvidenceTests(unittest.TestCase):
    def test_exactly_seventy_passes_with_explained_weights(self):
        result = resume_match.assess(JD, CV, review=review())
        self.assertEqual(result['score'], 70)
        self.assertIsNone(result['reason'])
        self.assertEqual([c['weight'] for c in result['criteria']], [3, 3, 2, 2])

    def test_partial_credit_is_not_full_credit(self):
        value = review()
        value['criteria'][0]['verdict'] = 'partial'
        result = resume_match.assess(JD, CV, review=value)
        self.assertEqual(result['score'], 55)
        self.assertEqual(result['reason'], 'resume_match_below_threshold')

    def test_preferred_skill_does_not_penalize(self):
        value = review()
        value['criteria'].append(criterion('React bonus', 'React preferred.', '', 'preferred', 'not_evidenced'))
        self.assertEqual(resume_match.assess(JD, CV, review=value)['score'], 70)

    def test_one_real_requirement_needs_no_three_technology_minimum(self):
        value = {'complete': True, 'criteria': [criterion('Payments', 'Build payments.', 'Built payments.')], 'blockers': []}
        self.assertEqual(resume_match.assess(JD, CV, review=value)['score'], 100)

    def test_no_assessment_means_pending_not_a_keyword_score(self):
        result = resume_match.assess('Ruby Rails Ruby Rails', 'Ruby Rails')
        self.assertIsNone(result['score'])
        self.assertEqual(result['reason'], 'resume_match_unknown')

    def test_fabricated_jd_or_cv_quotes_rejected(self):
        for field in ('jd_quote', 'cv_quote'):
            value = review()
            value['criteria'][0][field] = 'Unquoted invented fact'
            with self.assertRaises(ValueError):
                resume_match.assess(JD, CV, review=value)

    def test_incomplete_or_duplicate_assessment_rejected(self):
        value = review()
        value['complete'] = False
        with self.assertRaises(ValueError):
            resume_match.assess(JD, CV, review=value)
        value = review()
        value['criteria'].append(copy.deepcopy(value['criteria'][0]))
        with self.assertRaises(ValueError):
            resume_match.assess(JD, CV, review=value)

    def test_blockers_do_not_get_laundered_into_high_score(self):
        value = review()
        value['blockers'] = [{'kind': 'legal', 'jd_quote': 'US authorization required.',
                              'verdict': 'unknown', 'explanation': 'Ask the user; residence is not authorization.'}]
        result = resume_match.assess(JD + ' US authorization required.', CV, review=value)
        self.assertEqual(result['score'], 70)
        self.assertEqual(result['blockers'][0]['verdict'], 'unknown')

    def test_config_validation(self):
        for value in [True, -1, 101, float('nan'), '70']:
            with self.assertRaises(ValueError):
                resume_match.options({'resume_match': {'minimum_percent': value}})

    def test_model_cannot_supply_its_own_percentage(self):
        value = review()
        value['score'] = 100
        value['criteria'][0]['weight'] = 9999
        self.assertEqual(resume_match.assess(JD, CV, review=value)['score'], 70)

    def test_generation_never_calls_paid_models(self):
        with patch('cv_profile.free_models', return_value=[('paid/model', False)]), \
                patch('cv_profile.request') as request:
            with self.assertRaises(ValueError):
                resume_match.model_review(JD, CV, 'unused-test-key')
            request.assert_not_called()

    def test_model_uses_full_documents_and_validates_its_quotes(self):
        response = {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(review())}}]}
        with patch('cv_profile.free_models', return_value=[('test/model:free', False)]), \
                patch('cv_profile.request', return_value=response) as request:
            value, reviewer = resume_match.model_review(JD, CV, 'unused-test-key')
        payload = request.call_args.args[2]
        self.assertEqual(json.loads(payload['messages'][1]['content']), {'jd': JD, 'cv': CV})
        self.assertEqual(reviewer, 'openrouter:test/model:free')
        self.assertEqual(resume_match.assess(JD, CV, review=value)['score'], 70)


if __name__ == '__main__':
    unittest.main()
