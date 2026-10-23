"""Offline unit tests. Run: python -m unittest discover -s tests -v."""
import copy
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

# Importing apply creates data/. Keep tests away from real application history.
TEST_HOME = tempfile.TemporaryDirectory(prefix="autoapply-unit-")
with patch.dict(os.environ, {"AUTOAPPLY_HOME": TEST_HOME.name}):
    import apply
    import sources


class SafetyTest(unittest.TestCase):
    def setUp(self):
        cfg = copy.deepcopy(apply.CFG)
        self.enterContext(patch.object(apply, "CFG", cfg))
        self.enterContext(patch.object(apply, "PROFILE", cfg["profile"]))
        self.enterContext(patch.object(apply, "SETTINGS", {
            "english": "3", "salary": "60000", "currency": "USD", "notice": "0",
        }))
        self.enterContext(patch.object(apply, 'ATTEMPTS', {'total': 0, 'by_source': {}}))
        self.enterContext(patch.object(apply, 'METRICS', apply.RunMetrics()))
        self.enterContext(patch.object(apply, "pause"))
        self.enterContext(patch.object(apply, "log"))
        self.enterContext(patch.object(apply, "screenshot"))


class AnswerTests(SafetyTest):
    def test_unknown_questions_have_no_default_answer(self):
        for q in ["Have you ever been convicted of fraud?", "Do you have a driving licence?", ""]:
            with self.subTest(question=q):
                self.assertEqual(apply.answer(q), [])
                self.assertIsNone(apply.choose_option(q, ["Yes", "No"]))

    def test_unknown_numeric_fields_are_not_experience(self):
        for q in ["What is your age?", "How many dependants do you have?", "Years of Rust experience?"]:
            self.assertEqual(apply.answer(q, numeric=True), [])

    def test_confirmed_skill_years_only_match_experience_questions(self):
        apply.CFG["skill_years"] = {"python": 3, "java": 0}
        self.assertEqual(apply.answer("Years of Python experience?", numeric=True), ["3"])
        self.assertEqual(apply.answer("Years of Java experience?", numeric=True), ["0"])
        self.assertEqual(apply.answer("Years of JavaScript experience?", numeric=True), [])
        self.assertEqual(apply.answer("Your Python assessment score?", numeric=True), [])
        self.assertEqual(apply.choose_option("Years of Python experience?", ["0-1 years", "2-4 years"]), 1)

    def test_missing_range_does_not_choose_nearest_or_wrong_boundary(self):
        self.assertIsNone(apply.choose_years(10, [(0, "0-1 years"), (1, "2-3 years")]))
        self.assertEqual(apply.choose_years(1, [(0, "Less than 1 year"), (1, "1-3 years")]), 1)
        self.assertIsNone(apply.choose_years(3, [(0, "0-3 years"), (1, "3-5 years")]))

    def test_matching_is_exact_and_unambiguous(self):
        self.assertIsNone(apply.match_option(["No"], ["No experience", "North America"]))
        self.assertIsNone(apply.match_option(["Yes"], ["Yes", "YES"]))
        self.assertEqual(apply.match_option(["No"], ["Yes", " NO "]), 1)

    def test_sensitive_answers_are_not_inferred_even_from_broad_rules(self):
        apply.CFG["answers"].insert(0, {"match": ".*", "answer": "Yes"})
        for q in ["Will you require visa sponsorship for remote work?", "Are you authorized to work in Turkey?",
                  "Do you have an English degree?", "I agree to the privacy terms", "Can you relocate?"]:
            self.assertEqual(apply.answer(q), [])

    def test_exact_confirmed_answer_can_be_configured(self):
        apply.CFG["confirmed_answers"] = {"Will you require visa sponsorship?": "Yes"}
        self.assertEqual(apply.choose_option("Will you require visa sponsorship?", ["No", "Yes"]), 1)
        self.assertIsNone(apply.choose_option("Will you require visa sponsorship in the future?", ["No", "Yes"]))

    def test_known_salary_english_notice_and_cover_letter_still_work(self):
        apply.PROFILE["cover_letter"] = "My reviewed cover letter."
        self.assertEqual(apply.answer("Annual salary in USD?", numeric=True), ["60000"])
        self.assertEqual(apply.answer("Notice period", numeric=True), ["0"])
        self.assertIn("Intermediate", apply.answer("English level"))
        self.assertEqual(apply.answer("Cover letter"), ["My reviewed cover letter."])
        self.assertEqual(apply.answer("Explain your most difficult technical project"), [])

    def test_non_numeric_answers_are_not_stripped_into_numbers(self):
        apply.CFG["answers"] = [{"match": "availability", "answer": "2 weeks"}]
        self.assertEqual(apply.answer("Availability", numeric=True), [])

    def test_english_level_does_not_assert_native_proficiency(self):
        self.assertIsNone(apply.choose_option("Are you a native English speaker?", ["Yes", "No"]))

    def test_narrative_questions_do_not_get_keyword_based_profile_answers(self):
        for q in ["Describe your English communication experience", "Tell us about your GitHub projects",
                  "Explain how your degree meets the requirements"]:
            self.assertEqual(apply.answer(q), [])
            self.assertIsNone(apply.standard_answer(MagicMock(), q))

    def test_residence_does_not_create_authorization_or_commute_rules(self):
        profile = {"first_name": "Test", "last_name": "User", "email": "test@example.com",
                   "phone_intl": "", "city": "Berlin", "country": "Germany", "linkedin": "", "github": "",
                   "current_title": "Developer", "total_years": 4, "job_titles": ["Software Engineer"],
                   "title_keywords": ["software"], "skills": [{"skill": "Python", "years": 8}], "cover_letter": "Hello"}
        apply.CFG["skill_years"] = {"python": 3}
        previous = copy.deepcopy(apply.CFG["answers"])
        apply.apply_cv_profile(profile)
        self.assertEqual(apply.CFG["answers"], previous)
        self.assertEqual(apply.CFG["skill_years"], {"python": 3})
        self.assertEqual(apply.answer("Are you authorized to work in Germany?"), [])


def node():
    """An empty locator tree, suitable for offline control-flow tests."""
    n = MagicMock()
    n.first = n.last = n
    n.locator.return_value = n
    n.get_by_role.return_value = n
    n.filter.return_value = n
    n.and_.return_value = n
    n.all.return_value = []
    n.count.return_value = 1
    n.evaluate_all.return_value = []
    n.evaluate.return_value = []
    n.inner_text.return_value = ""
    n.get_attribute.return_value = ""
    return n


class ControlTests(SafetyTest):
    def test_unknown_combobox_is_not_opened_or_filled(self):
        field = MagicMock()
        field.get_attribute.return_value = ""
        self.assertFalse(apply.fill_combobox(MagicMock(), field, "Have you been convicted?"))
        field.click.assert_not_called()
        field.fill.assert_not_called()

    def test_combobox_does_not_choose_first_location_suggestion(self):
        apply.PROFILE["city"] = "Berlin, Germany"
        field = MagicMock()
        field.get_attribute.return_value = ""
        field.evaluate.return_value = True
        page = MagicMock()
        page.locator.return_value.all_inner_texts.return_value = ["Berlin, New Hampshire"]
        self.assertFalse(apply.fill_combobox(page, field, "City"))
        page.locator.return_value.nth.assert_not_called()

    def test_unknown_yesno_is_reported_even_if_preselected(self):
        scope = MagicMock()
        scope.evaluate.return_value = [("Have you been convicted?", True)]
        self.assertEqual(apply.fill_yesno(scope), ["Have you been convicted?"])
        scope.locator.assert_not_called()

    def test_known_yesno_selects_the_matching_answer(self):
        apply.CFG["confirmed_answers"] = {"Have you been convicted?": "No"}
        scope = MagicMock()
        scope.evaluate.return_value = [("Have you been convicted?", False)]
        group = scope.locator.return_value
        group.evaluate.side_effect = [{"question": "Have you been convicted?", "states": [True, False]},
                                      {"question": "Have you been convicted?", "states": [False, True]}]
        group.locator.return_value.nth.return_value.evaluate.return_value = True
        self.assertEqual(apply.fill_yesno(scope), [])
        group.locator.return_value.nth.assert_called_once_with(1)

    def test_unknown_checkbox_group_does_not_guess_from_skills_or_first_option(self):
        apply.CFG["skill_years"] = {"python": 3}
        scope = MagicMock()
        scope.evaluate.return_value = [("What certifications do you hold?", ["Python", "Other"], False)]
        self.assertEqual(apply.fill_checkgroups(scope), ["What certifications do you hold?"])
        scope.locator.assert_not_called()

    def test_unknown_radios_are_reported(self):
        scope, group = MagicMock(), node()
        scope.locator.return_value.all.return_value = [group]
        group.evaluate.return_value = ["Do you have a driving licence?", ["Yes", "No"]]
        self.assertEqual(apply.fill_radios(scope), ["Do you have a driving licence?"])
        group.click.assert_not_called()

    def test_unknown_textarea_is_not_filled_with_cover_letter(self):
        field, scope, page = node(), node(), node()
        field.input_value.return_value = ""
        field.is_editable.return_value = True
        field.get_attribute.return_value = ""
        field.evaluate.return_value = "Explain your difficult project"
        scope.locator.side_effect = lambda selector: fields if "textarea:visible" in selector else field if selector.startswith('[data-autoapply-field=') else empty
        fields, empty = node(), node()
        fields.all.return_value = [field]
        fields.evaluate_all.return_value = [{'selector': '[data-autoapply-field="fixture"]', 'question': 'Explain your difficult project'}]
        scope.evaluate.return_value = []
        apply.PROFILE["cover_letter"] = "Generic cover letter"
        self.assertIn("Explain your difficult project", apply.fill_step(page, scope, external=True))
        field.fill.assert_not_called()


class SubmissionGuardTests(SafetyTest):
    def setUp(self):
        super().setUp()
        self.manual = self.enterContext(patch.object(apply, "record_manual"))
        self.enterContext(patch.object(apply, "DRY_RUN", False))
        self.enterContext(patch.object(sources, "core", apply))
        self.form, self.page = node(), node()
        empty = node()
        empty.count.return_value = 0
        self.form.locator.side_effect = lambda selector: empty if selector == "input#follow-company-checkbox:checked" else self.form
        self.form.evaluate.side_effect = lambda script, *args: True if script == apply.NON_SUBMITTING_CONTROL_JS else []
        self.page.locator.return_value = self.form
        self.page.get_by_role.return_value = self.form
        self.page.url = "https://wellfound.com/jobs/123"
        self.page.goto.side_effect = lambda url, **kwargs: setattr(self.page, "url", url)
        self.page.evaluate.return_value = True
        self.page.context.pages = [self.page]
        self.job = {"id": "123", "title": "Engineer", "company": "Example", "url": self.page.url}

    def test_known_form_can_proceed(self):
        self.assertFalse(apply.require_manual_review(self.page, self.form, [], "123", "Engineer", "Example"))
        self.manual.assert_not_called()

    def test_required_missing_field_blocks_without_unanswered_list(self):
        self.form.evaluate_all.return_value = ["required resume"]
        self.assertTrue(apply.require_manual_review(self.page, self.form, [], "123", "Engineer", "Example"))
        self.manual.assert_called_once()

    def test_inspection_failure_blocks(self):
        self.form.evaluate_all.side_effect = RuntimeError("cannot inspect")
        self.assertTrue(apply.require_manual_review(self.page, self.form, [], "123", "Engineer", "Example"))

    def test_linkedin_blocks_before_submit_and_records_manual_job(self):
        with patch.object(apply, "fill_step", return_value=["Unknown question"]), patch.object(apply, "discard"):
            self.assertEqual(apply.apply_to_job(self.page, "123", "Engineer", "Example"), "manual_review")
        self.assertEqual(self.form.click.call_count, 1)  # Opening Easy Apply only.
        self.manual.assert_called_once()

    def test_linkedin_retry_also_checks_unknown_questions(self):
        self.form.inner_text.return_value = "Continue"
        self.form.get_attribute.return_value = "Continue"
        with patch.object(apply, "fill_step", side_effect=[[], ["New conditional question"]]), \
                patch.object(apply, "discard"):
            self.assertEqual(apply.apply_to_job(self.page, "123"), "manual_review")
        self.assertEqual(self.form.click.call_count, 2)  # Open, then continue; no retry click.

    def test_external_forms_preserve_unknown_questions_from_first_pass(self):
        self.enterContext(patch.object(apply, "EXTERNAL", True))
        self.enterContext(patch.object(apply, "EXT_STATE", {"count": 0, "last": 0.0}))
        with patch.object(apply, "ats_apply_url", return_value=("https://jobs.lever.co/example/apply", "lever")), \
                patch.object(apply, "fill_step", side_effect=[["Unknown question"], []]):
            self.assertEqual(apply.apply_form(self.page, "123", "Engineer", "Example", self.page.url), "manual_review")
        self.form.click.assert_not_called()
        self.manual.assert_called_once()

    def test_wellfound_blocks_and_does_not_prefill_all_textareas(self):
        with patch.object(sources, "wellfound_login", return_value=True), patch.object(sources, "goto", return_value=True), \
                patch.object(apply, "fill_step", return_value=["Unknown question"]):
            self.assertEqual(sources.wellfound_apply(self.page, self.job), "manual_review")
        self.assertEqual(self.form.click.call_count, 1)
        self.form.fill.assert_not_called()
        self.manual.assert_called_once()

    def test_indeed_blocks_before_continue_or_submit(self):
        with patch.object(sources, "goto", return_value=True), patch.object(apply, "wait_human", return_value=True), \
                patch.object(apply, "fill_step", return_value=["Unknown question"]):
            self.assertEqual(sources.indeed_apply(self.page, self.job), "manual_review")
        self.assertEqual(self.form.click.call_count, 1)
        self.manual.assert_called_once()

    def test_known_forms_reach_dry_run_on_all_submitters(self):
        self.enterContext(patch.object(apply, "DRY_RUN", True))
        self.enterContext(patch.object(apply, "EXTERNAL", True))
        self.enterContext(patch.object(apply, "EXT_STATE", {"count": 0, "last": 0.0}))
        self.form.inner_text.return_value = "Submit your application"
        with patch.object(apply, "fill_step", return_value=[]), patch.object(apply, "discard"), \
                patch.object(sources, "goto", return_value=True), \
                patch.object(sources, "wellfound_login", return_value=True), \
                patch.object(apply, "wait_human", return_value=True), \
                patch.object(apply, "ats_apply_url", return_value=("https://jobs.lever.co/example/apply", "lever")):
            self.assertEqual(apply.apply_to_job(self.page, "123"), "dry_run")
            self.assertEqual(apply.apply_form(self.page, "123", "Engineer", "Example", self.page.url), "dry_run")
            self.page.url = self.job["url"]
            self.assertEqual(sources.wellfound_apply(self.page, self.job), "dry_run")
            self.assertEqual(sources.indeed_apply(self.page, self.job), "dry_run")
        self.manual.assert_not_called()

    def test_manual_review_status_is_not_automatically_retried(self):
        self.assertFalse(apply.can_retry("manual_review"))


@unittest.skipUnless(os.getenv("AUTOAPPLY_BROWSER_TESTS") == "1", "optional local Chrome DOM tests")
class BrowserFormTests(SafetyTest):
    """Real DOM checks against local HTML only; no job sites or network access."""
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

    def form(self, html):
        self.page.set_content("<form>" + html + "</form>")
        return self.page.locator("form")

    def test_unknown_number_and_open_question_stay_empty(self):
        form = self.form('<label for="age">What is your age?</label><input id="age" type="number" required>'
                         '<label for="project">Explain your hardest project</label><textarea id="project"></textarea>')
        apply.PROFILE["cover_letter"] = "Reviewed cover letter"
        unknown = apply.fill_step(self.page, form, external=True)
        self.assertIn("What is your age?", unknown)
        self.assertIn("Explain your hardest project", unknown)
        self.assertEqual(self.page.locator("#age").input_value(), "")
        self.assertEqual(self.page.locator("#project").input_value(), "")
        self.assertTrue(apply.require_manual_review(self.page, form, unknown, "test", "Engineer", "Example"))

    def test_confirmed_skill_and_english_are_filled(self):
        apply.CFG["skill_years"] = {"python": 3}
        form = self.form('<label for="years">Years of Python experience?</label><input id="years" type="number" required>'
                         '<label for="english">English level</label><select id="english" required>'
                         '<option value="">Select an option</option><option>Basic</option><option>Intermediate</option></select>')
        unknown = apply.fill_step(self.page, form)
        self.assertEqual(unknown, [])
        self.assertEqual(self.page.locator("#years").input_value(), "3")
        self.assertEqual(self.page.locator("#english").input_value(), "Intermediate")
        self.assertFalse(apply.require_manual_review(self.page, form, unknown, "test", "Engineer", "Example"))

    def test_unknown_default_select_is_not_trusted(self):
        form = self.form('<label for="fraud">Have you been convicted of fraud?</label>'
                         '<select id="fraud"><option>Yes</option><option>No</option></select>')
        self.assertIn("Have you been convicted of fraud?", apply.fill_step(self.page, form))

    def test_unknown_radio_and_checkgroup_are_not_selected(self):
        form = self.form('<fieldset><legend>Do you have a driving licence?</legend>'
                         '<label><input type="radio" name="licence" value="yes">Yes</label>'
                         '<label><input type="radio" name="licence" value="no">No</label></fieldset>'
                         '<fieldset><legend>What certifications do you hold?</legend>'
                         '<label><input type="checkbox" name="cert" value="python">Python</label>'
                         '<label><input type="checkbox" name="cert" value="other">Other</label></fieldset>')
        apply.CFG["skill_years"] = {"python": 3}
        unknown = apply.fill_step(self.page, form)
        self.assertIn("Do you have a driving licence?", unknown)
        self.assertIn("What certifications do you hold?", unknown)
        self.assertEqual(form.locator("input:checked").count(), 0)

    def test_consent_requires_exact_confirmation(self):
        form = self.form('<label for="consent">I agree to privacy terms</label>'
                         '<input id="consent" type="checkbox" required>')
        unknown = apply.fill_step(self.page, form)
        self.assertIn("I agree to privacy terms", unknown)
        self.assertFalse(form.locator("#consent").is_checked())
        apply.CFG["confirmed_answers"] = {"I agree to privacy terms": "Yes"}
        self.assertEqual(apply.fill_step(self.page, form), [])
        self.assertTrue(form.locator("#consent").is_checked())

    def test_confirmed_checkgroup_removes_unconfirmed_preselected_options(self):
        form = self.form('<fieldset><legend>Which tools do you use?</legend>'
                         '<label><input type="checkbox" value="python">Python</label>'
                         '<label><input type="checkbox" value="rust" checked>Rust</label></fieldset>')
        apply.CFG["confirmed_answers"] = {"Which tools do you use?": ["Python"]}
        self.assertEqual(apply.fill_step(self.page, form), [])
        self.assertTrue(form.locator('[value="python"]').is_checked())
        self.assertFalse(form.locator('[value="rust"]').is_checked())

    def test_sensitive_text_question_cannot_use_standard_email_answer(self):
        form = self.form('<label for="email">I certify that this email address belongs to my employer</label>'
                         '<input id="email" type="text" required>')
        apply.PROFILE["email"] = "test@example.com"
        self.assertTrue(apply.fill_step(self.page, form, external=True))
        self.assertEqual(form.locator("#email").input_value(), "")

    def test_required_hidden_file_and_unsupported_fields_block_submission(self):
        form = self.form('<input type="file" name="resume" style="display:none" required>'
                         '<input type="date" name="start" required>')
        self.assertTrue(apply.require_manual_review(self.page, form, [], "test", "Engineer", "Example"))
        self.manual.assert_called_once()


if __name__ == "__main__":
    unittest.main()
