"""Profile schema, free-model fallback and UI/cache trust-boundary tests (offline)."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from test_answer_safety import SafetyTest
import apply
import cv_profile
import gui


def profile():
    return {**cv_profile.empty_profile(), "first_name": "Test", "last_name": "User", "email": "test@example.com",
            "phone_intl": "+1 555 123 4567", "city": "Toronto", "country": "Canada", "current_title": "Developer",
            "total_years": 3, "job_titles": ["Software Engineer"], "title_keywords": ["software"],
            "skills": [{"skill": "Python", "years": 3}], "cover_letter": "My reviewed cover letter."}


class SchemaTests(unittest.TestCase):
    def test_valid_profile_is_returned_without_mutation_and_valid_is_boolean(self):
        value = profile()
        original = copy.deepcopy(value)
        self.assertIs(cv_profile.validate(value), value)
        self.assertIs(cv_profile.valid(value), True)
        self.assertEqual(value, original)

    def test_non_objects_are_rejected_without_crashing_boolean_check(self):
        for value in [None, False, [], "profile", 3]:
            self.assertIs(cv_profile.valid(value), False)
            with self.assertRaises(cv_profile.ProfileValidationError):
                cv_profile.validate(value)

    def test_every_required_field_is_checked(self):
        for field in cv_profile.SCHEMA["required"]:
            value = profile()
            del value[field]
            with self.subTest(field=field), self.assertRaisesRegex(cv_profile.ProfileValidationError, field):
                cv_profile.validate(value)

    def test_all_string_fields_require_strings(self):
        for field, rule in cv_profile.SCHEMA["properties"].items():
            if rule["type"] != "string":
                continue
            for bad in [None, [], {}, 12, True]:
                value = profile()
                value[field] = bad
                with self.subTest(field=field, bad=bad):
                    self.assertFalse(cv_profile.valid(value))

    def test_years_are_strict_integers_in_range(self):
        for bad in [-1, 81, True, "3", 3.0, None, float("nan"), float("inf")]:
            for field in ["total_years", "skills"]:
                value = profile()
                if field == "skills":
                    value[field][0]["years"] = bad
                else:
                    value[field] = bad
                self.assertFalse(cv_profile.valid(value), (field, bad))
        for boundary in [0, 80]:
            value = profile()
            value.update(total_years=boundary, skills=[{"skill": "Python", "years": boundary}])
            self.assertTrue(cv_profile.valid(value))

    def test_array_types_and_elements_are_checked(self):
        for field in ["job_titles", "title_keywords", "skills"]:
            for bad in [None, "not an array", {}, [None], [True]]:
                value = profile()
                value[field] = bad
                self.assertFalse(cv_profile.valid(value), (field, bad))
        for bad in [[], [""], ["   "], ["Engineer", 42]]:
            value = profile()
            value["job_titles"] = bad
            self.assertFalse(cv_profile.valid(value))

    def test_skill_objects_require_exact_structure(self):
        for bad in [{}, {"skill": "Python"}, {"years": 3}, {"skill": "", "years": 3},
                    {"skill": "Python", "years": 3, "extra": "untrusted"}]:
            value = profile()
            value["skills"] = [bad]
            self.assertFalse(cv_profile.valid(value))
        value = profile()
        value["unexpected"] = "untrusted"
        self.assertFalse(cv_profile.valid(value))

    def test_array_text_items_are_nonblank_and_bounded(self):
        for field, bad in [("job_titles", "x" * 201), ("title_keywords", "x" * 101),
                           ("title_keywords", "  "), ("skills", "x" * 201), ("skills", "  ")]:
            value = profile()
            value[field] = [{"skill": bad, "years": 3}] if field == "skills" else [bad]
            self.assertFalse(cv_profile.valid(value))

    def test_text_and_list_size_limits_match_schema(self):
        for field, rule in cv_profile.SCHEMA["properties"].items():
            value = profile()
            if rule["type"] == "string":
                value[field] = "x" * (rule["maxLength"] + 1)
            elif rule["type"] == "array":
                item = {"skill": "Python", "years": 3} if field == "skills" else "Engineer"
                value[field] = [item] * (rule["maxItems"] + 1)
            else:
                continue
            self.assertFalse(cv_profile.valid(value), field)

    def test_error_paths_do_not_echo_personal_data(self):
        value = profile()
        value["skills"][0]["years"] = "PRIVATE-SECRET"
        with self.assertRaises(cv_profile.ProfileValidationError) as error:
            cv_profile.validate(value)
        self.assertIn("profile.skills[0].years", str(error.exception))
        self.assertNotIn("PRIVATE-SECRET", str(error.exception))

    def test_empty_optional_information_is_allowed_but_not_empty_job_titles(self):
        value = cv_profile.empty_profile()
        self.assertFalse(cv_profile.valid(value))
        value["job_titles"] = ["Software Engineer"]
        self.assertTrue(cv_profile.valid(value))


class JSONTests(unittest.TestCase):
    def test_plain_and_fenced_json_are_supported(self):
        text = json.dumps(profile())
        for wrapped in [text, "```json\n" + text + "\n```"]:
            self.assertEqual(cv_profile.parse_json(wrapped), profile())

    def test_non_object_json_and_extra_prose_are_not_silently_unwrapped(self):
        for text in ["[" + json.dumps(profile()) + "]", "null", "false", "123", "Here is your profile: " + json.dumps(profile())]:
            with self.assertRaises(ValueError):
                cv_profile.parse_json(text)

    def test_duplicates_non_finite_numbers_and_non_text_responses_are_rejected(self):
        for text in ['{"total_years": 3, "total_years": 4}', '{"skills": [{"years": 3, "years": 4}]}',
                     '{"total_years": NaN}', '{"total_years": Infinity}', None, {}, []]:
            with self.assertRaises(ValueError):
                cv_profile.parse_json(text)


class ExtractionTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(cv_profile, "cv_text", return_value="Resume text " * 30))
        self.enterContext(patch.object(cv_profile, "free_models", return_value=[("one:free", False), ("two:free", True)]))

    @staticmethod
    def response(value):
        return {"choices": [{"message": {"content": json.dumps(value)}}]}

    def test_invalid_first_model_falls_back_with_field_path(self):
        bad = profile()
        bad["skills"][0]["years"] = "PRIVATE-SECRET"
        messages = []
        with patch.object(cv_profile, "request", side_effect=[self.response(bad), self.response(profile())]) as request:
            self.assertEqual(cv_profile.extract(Path("unused.pdf"), "key", log=messages.append), profile())
        self.assertEqual(request.call_count, 2)
        self.assertIn("profile.skills[0].years", messages[0])
        self.assertNotIn("PRIVATE-SECRET", messages[0])
        body = request.call_args.args[2]
        self.assertEqual(body["response_format"]["json_schema"]["schema"], cv_profile.SCHEMA)

    def test_structured_output_is_still_validated_locally(self):
        self.enterContext(patch.object(cv_profile, "free_models", return_value=[("one:free", True), ("two:free", True)]))
        bad = {**profile(), "total_years": True}
        with patch.object(cv_profile, "request", side_effect=[self.response(bad), self.response(profile())]) as request:
            self.assertEqual(cv_profile.extract(Path("unused.pdf"), "key", log=lambda _: None), profile())
        self.assertEqual(request.call_count, 2)

    def test_bad_envelopes_and_non_object_profiles_fall_back(self):
        for bad in [None, {"choices": None}, {"choices": []}, self.response([profile()])]:
            with patch.object(cv_profile, "request", side_effect=[bad, self.response(profile())]) as request:
                self.assertEqual(cv_profile.extract(Path("unused.pdf"), "key", log=lambda _: None), profile())
            self.assertEqual(request.call_count, 2)

    def test_all_invalid_models_fail_without_returning_partial_profile(self):
        with patch.object(cv_profile, "request", return_value=self.response({**profile(), "job_titles": "Engineer"})):
            with self.assertRaisesRegex(RuntimeError, "profile.job_titles"):
                cv_profile.extract(Path("unused.pdf"), "key", log=lambda _: None)


class ProfileBoundaryTests(SafetyTest):
    def setUp(self):
        super().setUp()
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="profile-validation-")))
        self.resume = self.home / "resume.pdf"
        self.resume.write_bytes(b"test resume")
        apply.SETTINGS.update(resume=str(self.resume), notice="14")
        self.enterContext(patch.object(gui, "STATE", {"running": False, "analyzing": False}))
        self.enterContext(patch.object(gui, "ACTIVITY_LOCK", threading.Lock()))
        self.enterContext(patch.object(apply, "find_key", return_value="test-key"))
        self.thread = self.enterContext(patch.object(gui.threading, "Thread"))
        self.enterContext(patch.object(apply, "DRY_RUN", False))
        self.enterContext(patch.object(apply, "LOG_LINES", []))

    def body(self):
        # These hidden fields are intentionally not sent by the actual UI.
        visible = {k: v for k, v in profile().items() if k not in ("skills", "title_keywords", "current_title")}
        visible["total_years"] = "3"
        return {"profile": visible, "cv_hash": hashlib.sha256(self.resume.read_bytes()).hexdigest(),
                "english": "3", "notice": "14", "locations": ["Worldwide"],
                "sources": ["linkedin"], "test_mode": True, "currency": "USD", "salary": "5000",
                "salary_eur": "4500", "salary_usd": "5000"}

    def test_invalid_gui_profile_does_not_change_settings_config_or_start_worker(self):
        original_settings, original_cfg = copy.deepcopy(apply.SETTINGS), copy.deepcopy(apply.CFG)
        for key, value in [("job_titles", "Engineer"), ("first_name", ["Test"]), ("total_years", "3.5"),
                           ("total_years", "-1"), ("total_years", "81"), ("total_years", True),
                           ("total_years", 3.0), ("total_years", "invalid"), ("skills", [{"skill": "Python", "years": "many"}])]:
            body = self.body()
            body["profile"][key] = value
            with patch.object(apply, "finalize_settings") as finalize:
                result = gui.start(body)
                self.assertEqual(result["error"], "invalid_profile", (key, value))
                self.assertIn("profile.", result["detail"])
                finalize.assert_not_called()
            self.assertEqual(apply.SETTINGS, original_settings)
            self.assertEqual(apply.CFG, original_cfg)
        self.thread.assert_not_called()

    def test_gui_rejects_non_object_and_unexpected_fields(self):
        for body in [None, [], {"profile": None}, {"profile": []}]:
            self.assertEqual(gui.start(body)["error"], "invalid_profile")
        body = self.body()
        body["profile"]["unexpected"] = "untrusted"
        self.assertEqual(gui.start(body)["error"], "invalid_profile")
        self.thread.assert_not_called()

    def test_empty_titles_and_missing_identity_keep_existing_ui_errors(self):
        body = self.body()
        body["profile"]["job_titles"] = []
        self.assertEqual(gui.start(body)["error"], "no_titles")
        body = self.body()
        body["profile"]["first_name"] = ""
        self.assertEqual(gui.start(body), {"error": "missing", "fields": ["first_name"]})

    def test_valid_manual_gui_profile_gets_complete_schema_and_integer_years(self):
        with patch.object(apply, "finalize_settings") as finalize:
            self.assertEqual(gui.start(self.body()), {"ok": True})
        stored = apply.SETTINGS["cv_profile"]
        self.assertTrue(cv_profile.valid(stored))
        self.assertEqual(stored["total_years"], 3)
        self.assertEqual(stored["skills"], [])
        self.assertEqual(stored["current_title"], "")
        finalize.assert_called_once()
        self.thread.return_value.start.assert_called_once()

    def test_explicit_phone_parts_replace_old_unsplit_phone_without_guessing(self):
        body = self.body()
        body['profile']['phone_intl'] = '+905551234567'
        body.update(phone_country_code='+90', phone_national='555 123 45 67')
        with patch.object(apply, 'finalize_settings'):
            self.assertEqual(gui.start(body), {'ok': True})
        stored = apply.SETTINGS['cv_profile']
        self.assertEqual(stored['phone_intl'], '+90 5551234567')
        apply.apply_cv_profile(stored)
        self.assertEqual(apply.PROFILE['phone'], '5551234567')
        self.assertEqual(apply.PROFILE['country_code'], '(+90)')
        self.assertEqual(apply.PROFILE['phone_intl'], '+90 5551234567')

    def test_invalid_or_partial_phone_parts_do_not_publish_settings(self):
        before = copy.deepcopy(apply.SETTINGS)
        for code, number in [('', '5551234567'), ('+90', ''), (None, '123'),
                             ('+000', '123'), ('+90', '+905551234567'), ('+90', '123x45'),
                             ('+90', '1' * 14)]:
            body = self.body()
            body.update(phone_country_code=code, phone_national=number)
            self.assertEqual(gui.start(body)['error'], 'invalid_profile')
            self.assertEqual(apply.SETTINGS, before)
        self.thread.assert_not_called()

    def test_question_presets_are_saved_with_cv_and_can_be_cleared(self):
        body = self.body()
        body['question_presets'] = [{'question': 'Will you require visa sponsorship?', 'answer': 'No'}]
        with patch.object(apply, 'finalize_settings'):
            self.assertEqual(gui.start(body), {'ok': True})
        self.assertEqual(apply.SETTINGS['question_presets'], body['question_presets'])
        self.assertEqual(apply.SETTINGS['question_presets_cv_hash'], body['cv_hash'])
        gui.clear_cv_profile()
        self.assertNotIn('question_presets', apply.SETTINGS)

    def test_invalid_question_presets_do_not_publish_settings(self):
        before = copy.deepcopy(apply.SETTINGS)
        body = self.body()
        body['question_presets'] = [{'question': 'Anything?', 'answer': True}]
        self.assertEqual(gui.start(body)['error'], 'invalid_settings')
        self.assertEqual(apply.SETTINGS, before)
        self.thread.assert_not_called()

    def test_gui_salary_rejects_invalid_values_without_changing_settings(self):
        before = copy.deepcopy(apply.SETTINGS)
        for field, value in [('currency', 'XYZ'), ('salary', '-5000'), ('salary', '5,000'),
                             ('salary', '0'), ('salary', 'NaN'), ('salary', True),
                             ('salary_usd', '5001')]:
            body = self.body()
            body[field] = value
            self.assertEqual(gui.start(body)['error'], 'invalid_settings', (field, value))
            self.assertEqual(apply.SETTINGS, before)
        self.thread.assert_not_called()

    def test_gui_cleared_salary_does_not_reuse_old_amounts(self):
        apply.SETTINGS['monthly_salary_by_currency'] = {'USD': 5000}
        body = self.body()
        body.update(salary='', salary_usd='', salary_eur='')
        with patch.object(apply, 'finalize_settings'):
            self.assertEqual(gui.start(body), {'ok': True})
        self.assertEqual(apply.SETTINGS['salary'], '')
        self.assertEqual(apply.SETTINGS['monthly_salary_by_currency'], {})
        self.assertEqual(apply.salary_answer('Annual salary in USD'), '')

    def test_gui_decimal_salary_is_preserved_not_digit_stripped(self):
        body = self.body()
        body.update(salary='5000.25', salary_usd='5000.25', salary_eur='')
        with patch.object(apply, 'finalize_settings'):
            self.assertEqual(gui.start(body), {'ok': True})
        self.assertEqual(apply.SETTINGS['salary'], '60003')
        self.assertEqual(apply.salary_answer('Monthly salary in USD'), '5000.25')

    def test_valid_cache_preserves_hidden_validated_fields(self):
        apply.SETTINGS["cv_profile"] = profile()
        apply.SETTINGS['cv_hash'] = hashlib.sha256(self.resume.read_bytes()).hexdigest()
        with patch.object(apply, "finalize_settings"):
            gui.start(self.body())
        self.assertEqual(apply.SETTINGS["cv_profile"]["skills"], profile()["skills"])
        self.assertEqual(apply.SETTINGS["cv_profile"]["current_title"], "Developer")

    def test_valid_gui_profile_is_saved_and_applied_before_worker_starts(self):
        settings_file = self.home / "settings.json"
        with patch.object(apply, "SETTINGS_FILE", settings_file):
            self.assertEqual(gui.start(self.body()), {"ok": True})
        saved = json.loads(settings_file.read_text())
        self.assertTrue(cv_profile.valid(saved["cv_profile"]))
        self.assertEqual(apply.PROFILE["full_name"], "Test User")
        self.assertEqual(apply.CFG["search"]["keywords"], ["Software Engineer"])
        self.thread.return_value.start.assert_called_once()

    def test_gui_blank_or_numeric_text_years_are_transport_normalized(self):
        for value, expected in [("", 0), (" 0 ", 0), ("80", 80)]:
            body = self.body()
            body["profile"]["total_years"] = value
            with patch.object(apply, "finalize_settings"):
                self.assertEqual(gui.start(body), {"ok": True})
            self.assertEqual(apply.SETTINGS["cv_profile"]["total_years"], expected)
            with patch.object(apply, "run_bot", return_value=0):
                self.thread.call_args.kwargs["target"]()

    def test_invalid_cache_is_not_served_to_js_and_manual_entry_can_replace_it(self):
        apply.SETTINGS["cv_profile"] = {**profile(), "skills": [None]}
        state = gui.state()
        self.assertIsNone(state["profile"])
        self.assertIn("profile.skills[0]", state["profile_error"])
        with patch.object(apply, "finalize_settings"):
            self.assertEqual(gui.start(self.body()), {"ok": True})
        self.assertEqual(apply.SETTINGS["cv_profile"]["skills"], [])

    def test_analyze_does_not_persist_invalid_extractor_output(self):
        apply.SETTINGS.update(cv_profile=profile(), cv_hash=hashlib.sha256(self.resume.read_bytes()).hexdigest())
        before = copy.deepcopy(apply.SETTINGS)
        with patch.object(cv_profile, "extract", return_value={**profile(), "job_titles": "Engineer"}):
            result = gui.analyze({"key": ""})
        self.assertEqual(result["error"], "analyze_failed")
        self.assertIn("profile.job_titles", result["detail"])
        self.assertEqual(apply.SETTINGS, before)
        self.assertFalse(gui.STATE["analyzing"])

    def test_valid_analyzed_profile_is_cached_with_matching_resume_hash(self):
        with patch.object(cv_profile, "extract", return_value=profile()):
            self.assertEqual(gui.analyze({"key": ""}), {"ok": True, "profile": profile(),
                "cv_hash": hashlib.sha256(self.resume.read_bytes()).hexdigest(), "cv_name": self.resume.name})
        self.assertEqual(apply.SETTINGS["cv_hash"], hashlib.sha256(self.resume.read_bytes()).hexdigest())
        self.assertFalse(gui.STATE["analyzing"])

    def test_apply_and_finalize_reject_invalid_profile_before_any_mutation(self):
        bad = {**profile(), "first_name": "Changed", "skills": [{"skill": None, "years": 3}]}
        cfg = copy.deepcopy(apply.CFG)
        with self.assertRaises(cv_profile.ProfileValidationError):
            apply.apply_cv_profile(bad)
        self.assertEqual(apply.CFG, cfg)
        settings_file = self.home / "settings.json"
        settings_file.write_text("previous settings")
        apply.SETTINGS["cv_profile"] = bad
        with patch.object(apply, "SETTINGS_FILE", settings_file):
            with self.assertRaises(cv_profile.ProfileValidationError):
                apply.finalize_settings()
        self.assertEqual(settings_file.read_text(), "previous settings")
        self.assertEqual(apply.CFG, cfg)

    def test_valid_empty_fields_do_not_reuse_stale_profile_answers(self):
        apply.PROFILE.update(current_title="Previous title", cover_letter="Previous cover letter",
                             phone="123", country_code="(+1)")
        value = {**profile(), "current_title": "", "cover_letter": "", "phone_intl": ""}
        apply.apply_cv_profile(value)
        for field in ["current_title", "cover_letter", "phone", "country_code"]:
            self.assertEqual(apply.PROFILE[field], "")

    def test_direct_bot_start_rejects_invalid_saved_profile_before_browser(self):
        apply.SETTINGS["cv_profile"] = {**profile(), "total_years": -1}
        with patch.object(apply, "sync_playwright") as browser, patch.object(apply, "load_applied") as history:
            with self.assertRaises(cv_profile.ProfileValidationError):
                apply.run_bot()
        browser.assert_not_called()
        history.assert_not_called()

    def test_terminal_main_reports_invalid_profile_without_traceback_or_browser(self):
        with patch.object(apply, "ask_settings", side_effect=cv_profile.ProfileValidationError("profile.skills: expected array")), \
                patch.object(apply, "run_bot") as run:
            self.assertEqual(apply.main(), 1)
        run.assert_not_called()
        self.assertIn("profile.skills", apply.log.call_args.args[0])

    def test_invalid_terminal_cache_is_reanalyzed_before_display(self):
        apply.SETTINGS.update(cv_profile={**profile(), "job_titles": "Engineer"},
                              cv_hash=hashlib.sha256(self.resume.read_bytes()).hexdigest())
        with patch.object(apply, "api_key", return_value="key"), patch.object(cv_profile, "extract", return_value=profile()) as extract, \
                patch.object(apply, "ask", return_value="y"), patch.object(apply, "show_profile") as display, patch("builtins.print"):
            apply.cv_setup()
        extract.assert_called_once()
        display.assert_called_once()
        self.assertTrue(cv_profile.valid(apply.SETTINGS["cv_profile"]))

    def test_invalid_terminal_edit_keeps_last_valid_profile(self):
        apply.SETTINGS.update(cv_profile=profile(), cv_hash=hashlib.sha256(self.resume.read_bytes()).hexdigest())
        with patch.object(apply, "ask", side_effect=["t", "y"]), patch.object(apply, "show_profile"), \
                patch("builtins.input", return_value="x" * 201), patch("builtins.print"), patch.object(cv_profile, "extract") as extract:
            apply.cv_setup()
        extract.assert_not_called()
        self.assertEqual(apply.SETTINGS["cv_profile"]["job_titles"], ["Software Engineer"])
