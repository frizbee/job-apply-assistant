"""All-source date/remote gates, conservative parsing and local DOM fixtures."""
import datetime as dt
import json
import os
import unittest
from unittest.mock import MagicMock, patch

from test_answer_safety import SafetyTest, node
import apply
import job_metadata as metadata
import sources

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 10, 20, 12, tzinfo=UTC)


class ParsingTests(unittest.TestCase):
    def test_supported_dates_are_timezone_aware(self):
        for value in [NOW, NOW.isoformat(), "2026-10-20T12:00:00Z", NOW.timestamp(),
                      str(int(NOW.timestamp())), NOW.timestamp() * 1000,
                      "Tue, 20 Oct 2026 12:00:00 GMT"]:
            with self.subTest(value=value):
                self.assertEqual(metadata.posting_time(value, NOW), NOW)

    def test_invalid_or_missing_dates_are_unknown_not_fresh(self):
        for value in [None, "", "bad date", True, float("nan"), float("inf"),
                      NOW.replace(tzinfo=None), "2026-10-20T12:00:00", "30+ days ago"]:
            self.assertIsNone(metadata.posting_time(value, NOW))

    def test_relative_dates_use_oldest_end_of_rounded_interval(self):
        self.assertEqual(metadata.posting_time("Posted 6 days ago", NOW), NOW - dt.timedelta(days=7))
        self.assertEqual(metadata.posting_time("7 days ago", NOW), NOW - dt.timedelta(days=8))
        self.assertEqual(metadata.posting_time("Posted today", NOW), NOW - dt.timedelta(days=1))
        self.assertEqual(metadata.posting_time("2026-10-20", NOW), dt.datetime(2026, 10, 19, 10, tzinfo=UTC))

    def test_workplace_labels_are_not_description_keyword_searches(self):
        for value in ["Remote", "TELECOMMUTE", "Fully remote", "Remote - United States", True]:
            self.assertEqual(metadata.workplace_kind(value), "remote")
        for value in ["Hybrid", "Hybrid - London"]:
            self.assertEqual(metadata.workplace_kind(value), "hybrid")
        for value in ["On-site", "On site", False]:
            self.assertEqual(metadata.workplace_kind(value), "onsite")
        for value in ["Remote Sensing Engineer", "Hybrid Cloud Engineer", "We build software for remote teams", "Worldwide"]:
            self.assertIsNone(metadata.workplace_kind(value))
        self.assertEqual(metadata.workplace_kind("Remote", "Hybrid"), "hybrid")

    def test_exact_cutoff_future_and_missing_metadata(self):
        def reason(posted, workplace="remote", remote_only=True):
            return metadata.eligibility_reason({"posted_at": posted, "workplace": workplace}, 168, remote_only, NOW)
        self.assertIsNone(reason(NOW - dt.timedelta(days=7)))
        self.assertEqual(reason(NOW - dt.timedelta(days=7, seconds=1)), "posting_too_old")
        self.assertEqual(reason(NOW + dt.timedelta(seconds=1)), "posting_date_future")
        self.assertEqual(reason(None), "posting_date_unknown")
        self.assertEqual(reason(NOW, None), "workplace_unknown")
        self.assertEqual(reason(NOW, "hybrid"), "not_remote")
        self.assertEqual(reason(NOW, "onsite"), "not_remote")
        self.assertIsNone(reason(NOW, None, remote_only=False))

    def test_merge_cannot_turn_old_hybrid_job_into_recent_remote_job(self):
        combined = metadata.merge_metadata(
            {"posted_at": NOW - dt.timedelta(days=20), "workplace": "hybrid"},
            {"posted_at": NOW, "workplace": "remote", "location": "United States"})
        self.assertEqual(combined["posted_at"], NOW - dt.timedelta(days=20))
        self.assertEqual(combined["workplace"], "hybrid")


class GateTests(SafetyTest):
    def setUp(self):
        super().setUp()
        self.record = self.enterContext(patch.object(apply, "record"))
        self.manual = self.enterContext(patch.object(apply, "record_manual"))
        self.enterContext(patch.object(apply, "job_ok", return_value=None))
        self.enterContext(patch.object(apply, "DEADLINE", float("inf")))
        self.job = {"id": "test", "title": "Engineer", "company": "Example", "url": "https://example.com/job", "desc": "", "location": ""}

    def job_with(self, days=1, workplace="remote", known_date=True):
        return {**self.job, "metadata": {"posted_at": dt.datetime.now(UTC) - dt.timedelta(days=days) if known_date else None,
                                         "workplace": workplace}}

    def test_every_board_uses_same_final_gate(self):
        for name in sources.SOURCES:
            for days, workplace, known_date, allowed in [(1, "remote", True, True), (8, "remote", True, False),
                    (1, "onsite", True, False), (1, "hybrid", True, False), (1, None, True, False), (1, "remote", False, False)]:
                with self.subTest(source=name, days=days, workplace=workplace, known_date=known_date):
                    job = self.job_with(days, workplace, known_date)
                    send = MagicMock(return_value="applied")
                    page = node()
                    page.url = job["url"]
                    with patch.object(sources, "SOURCES", {name: lambda page: iter([(job, send)])}), \
                            patch.object(apply, "read_job_metadata", return_value={}):
                        count = sources.run(apply, page, {}, 0)
                    self.assertEqual(send.call_count, int(allowed))
                    self.assertEqual(count, int(allowed))

    def test_linkedin_recommendations_cannot_bypass_gate(self):
        for days, workplace, known_date, allowed in [(1, "remote", True, True), (8, "remote", True, False),
                (1, "hybrid", True, False), (1, "onsite", True, False), (1, None, True, False), (1, "remote", False, False)]:
            with self.subTest(days=days, workplace=workplace, known_date=known_date):
                with patch.object(apply, "job_info", return_value=("Engineer", "Example", "")), \
                        patch.object(apply, "read_job_metadata", return_value=self.job_with(days, workplace, known_date)["metadata"]), \
                        patch.object(apply, "apply_to_job", return_value="applied") as send:
                    self.assertEqual(apply.process_job(node(), {}, "123"), allowed)
                    self.assertEqual(send.call_count, int(allowed))

    def test_unknown_metadata_is_recorded_for_manual_processing(self):
        self.assertFalse(apply.check_job_eligibility(self.job_with(known_date=False), {}))
        self.assertEqual(self.record.call_args.args[4], "metadata_manual")
        self.assertEqual(self.manual.call_args.args[-1], "posting_date_unknown")

    def test_explicit_location_restriction_uses_common_policy(self):
        job = self.job_with()
        job["metadata"]["location"] = "Canada"
        apply.PROFILE["country"] = "Japan"
        self.assertFalse(apply.check_job_eligibility(job, {}))
        self.assertEqual(self.record.call_args.args[4], "location_restricted")
        apply.PROFILE["country"] = "Canada"
        self.assertTrue(apply.check_job_eligibility(job, {}))

    def test_unknown_feed_metadata_can_be_resolved_from_job_detail(self):
        job = self.job_with(known_date=False, workplace=None)
        send = MagicMock(return_value="applied")
        page = node()
        page.url = "https://example.com/search"
        with patch.object(sources, "SOURCES", {"indeed": lambda page: iter([(job, send)])}), \
                patch.object(sources, "goto", return_value=True) as goto, \
                patch.object(apply, "read_job_metadata", return_value=self.job_with()["metadata"]):
            self.assertEqual(sources.run(apply, page, {}, 0), 1)
        goto.assert_called_once_with(page, job["url"], "indeed")
        send.assert_called_once()

    def test_fresh_no_longer_accepts_missing_malformed_or_future_values(self):
        with patch.object(sources, "core", apply):
            for value in [None, "bad", dt.datetime.now(UTC) + dt.timedelta(days=1)]:
                self.assertFalse(sources.fresh(value))


class ExtractionTests(SafetyTest):
    def test_anonymous_jobposting_requires_matching_primary_title(self):
        page = node()
        page.url = "https://wellfound.com/jobs/123-engineer"
        raw = {"postings": [{"title": "Engineer", "datePosted": NOW.isoformat(), "jobLocationType": "TELECOMMUTE"}],
               "labels": [], "times": [], "title": "Other job"}
        page.evaluate.return_value = raw
        self.assertIsNone(metadata.page_metadata(page, "wellfound", page.url)["posted_at"])
        raw["title"] = "Engineer"
        self.assertEqual(metadata.page_metadata(page, "wellfound", page.url)["posted_at"], NOW)

    def test_other_indeed_job_in_jsonld_does_not_supply_metadata(self):
        page = node()
        page.url = "https://www.indeed.com/viewjob?jk=real"
        page.evaluate.return_value = {"postings": [{"url": "https://www.indeed.com/viewjob?jk=other",
            "datePosted": NOW.isoformat(), "jobLocationType": "TELECOMMUTE"}], "labels": [], "times": []}
        result = metadata.page_metadata(page, "indeed", page.url)
        self.assertIsNone(result["posted_at"])
        self.assertIsNone(result["workplace"])

    def test_linkedin_canonical_slug_can_identify_same_job(self):
        page = node()
        page.url = "https://www.linkedin.com/jobs/view/123/"
        page.evaluate.return_value = {"postings": [{"url": "https://www.linkedin.com/jobs/view/engineer-at-example-123/",
            "datePosted": NOW.isoformat(), "jobLocationType": "TELECOMMUTE"}], "labels": [], "times": []}
        self.assertEqual(metadata.page_metadata(page, "linkedin", page.url)["posted_at"], NOW)

    def test_redirect_to_login_is_not_job_metadata(self):
        page = node()
        page.url = "https://wellfound.com/login"
        page.evaluate.return_value = {"postings": [{"datePosted": NOW.isoformat(), "jobLocationType": "TELECOMMUTE"}]}
        self.assertEqual(metadata.page_metadata(page, "wellfound", "https://wellfound.com/jobs/123"), {})


@unittest.skipUnless(os.getenv("AUTOAPPLY_BROWSER_TESTS") == "1", "optional local Chrome DOM tests")
class BrowserMetadataTests(SafetyTest):
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
        self.html = ""
        self.context.route("**/*", lambda route: route.fulfill(content_type="text/html", body=self.html))
        self.page = self.context.new_page()

    def test_jsonld_graph_selects_current_job_not_recommendation(self):
        url = "https://wellfound.com/jobs/123-engineer"
        self.html = '<script type="application/ld+json">' + json.dumps({"@graph": [
            {"@type": "JobPosting", "url": "https://wellfound.com/jobs/other", "datePosted": "2026-10-20", "jobLocationType": "TELECOMMUTE"},
            {"@type": "JobPosting", "url": url, "datePosted": NOW.isoformat(), "jobLocationType": "HYBRID"}]}) + '</script>'
        self.page.goto(url)
        result = metadata.page_metadata(self.page, "wellfound", url)
        self.assertEqual(result["posted_at"], NOW)
        self.assertEqual(result["workplace"], "hybrid")

    def test_linkedin_header_metadata_ignores_remote_description_and_recommendations(self):
        url = "https://www.linkedin.com/jobs/view/123/"
        self.html = '<div class="job-details-jobs-unified-top-card"><p>On-site</p><p>Posted 2 days ago</p></div>' \
                    '<article>We build remote software. Remote</article><aside>Remote<br>Posted 1 hour ago</aside>'
        self.page.goto(url)
        result = metadata.page_metadata(self.page, "linkedin", url)
        self.assertEqual(result["workplace"], "onsite")
        self.assertIsNotNone(result["posted_at"])

    def test_missing_date_is_not_invented_from_unrelated_page_time(self):
        url = "https://www.indeed.com/viewjob?jk=real"
        self.html = '<div id="jobDetailsSection">Remote</div><aside><time datetime="2026-10-20T12:00:00Z">Today</time></aside>'
        self.page.goto(url)
        result = metadata.page_metadata(self.page, "indeed", url)
        self.assertEqual(result["workplace"], "remote")
        self.assertIsNone(result["posted_at"])

    def test_hidden_header_does_not_supply_remote_or_date_evidence(self):
        url = "https://www.linkedin.com/jobs/view/123/"
        self.html = '<div class="job-details-jobs-unified-top-card" style="display:none">Remote<br>Posted 1 day ago</div>'
        self.page.goto(url)
        result = metadata.page_metadata(self.page, "linkedin", url)
        self.assertIsNone(result["workplace"])
        self.assertIsNone(result["posted_at"])


class FeedTests(SafetyTest):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.object(sources, "core", apply))
        self.enterContext(patch.object(sources, "keyword_match", return_value=True))
        self.enterContext(patch.object(sources.time, "sleep"))

    def test_remoteok_carries_explicit_metadata_and_does_not_drop_unknown_dates(self):
        feed = [{"legal": "terms"}, {"id": "1", "position": "Engineer", "company": "Example",
                "url": "https://remoteok.com/jobs/1", "location": "Remote - United States", "epoch": None}]
        with patch.object(sources, "http", return_value=json.dumps(feed)):
            jobs = list(sources.remoteok(node()))
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0][0]["metadata"]["workplace"], "remote")
        self.assertIsNone(jobs[0][0]["metadata"]["posted_at"])

    def test_himalayas_string_date_and_hybrid_flag_are_preserved(self):
        feed = {"jobs": [{"guid": "https://himalayas.app/companies/example/jobs/1", "title": "Engineer",
                "applicationLink": "https://example.com/job", "pubDate": NOW.isoformat(),
                "workplaceType": "Hybrid", "locationRestrictions": ["United States"]}]}
        with patch.object(sources, "http", return_value=json.dumps(feed)):
            jobs = list(sources.himalayas(node()))
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0][0]["metadata"]["workplace"], "hybrid")
        self.assertEqual(metadata.posting_time(jobs[0][0]["metadata"]["posted_at"]), NOW)

    def test_wwr_unknown_date_is_forwarded_for_manual_policy_not_silently_accepted(self):
        feed = '<rss><channel><item><title>Example: Engineer</title><link>https://weworkremotely.com/jobs/1</link>' \
               '<pubDate>invalid</pubDate><region>Anywhere</region></item></channel></rss>'
        with patch.object(sources, "http", return_value=feed):
            jobs = list(sources.weworkremotely(node()))
        self.assertEqual(len(jobs), 1)
        self.assertIsNone(jobs[0][0]["metadata"]["posted_at"])
        self.assertIsNone(jobs[0][0]["metadata"]["workplace"])
