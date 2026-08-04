import json  # noqa: F401  (used by fixtures round-trip)
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import qa_gate  # noqa: E402

FIXTURES = os.path.join(ROOT, "tests", "fixtures")
POLICY = qa_gate.load_json(os.path.join(ROOT, "tenant_policy.json"))
AI_CACHE = os.path.join(ROOT, "ai_semantic_cache.json")


def evaluate(fixture_name, state=None):
    """Run the gate over a fixture with an isolated, in-memory state."""
    snapshot = qa_gate.load_json(os.path.join(FIXTURES, fixture_name))
    state = state if state is not None else {"state_version": 1, "entries": {}}
    reviewer = qa_gate.RecordedSemanticReviewer(AI_CACHE)
    return qa_gate.run(snapshot, POLICY, state, reviewer), state


def check_ids(report):
    return {f["check_id"] for f in report["findings"]}


class TestUrlRules(unittest.TestCase):
    """The narrow URL rules, which are the ones most able to do harm if wrong."""

    def test_rejects_non_https(self):
        _, _, err = qa_gate.parse_url("http://example.com/a")
        self.assertIn("https", err)

    def test_rejects_embedded_credentials(self):
        # Renders to a human as "antarcticfootballleague.com", resolves to attacker.example.
        _, _, err = qa_gate.parse_url("https://antarcticfootballleague.com@attacker.example/x")
        self.assertIn("credentials", err)

    def test_rejects_missing_host(self):
        _, _, err = qa_gate.parse_url("https:///just-a-path")
        self.assertIn("hostname", err)

    def test_accepts_valid_url(self):
        host, path, err = qa_gate.parse_url("https://Antarcticfootballleague.com./tickets")
        self.assertIsNone(err)
        self.assertEqual(host, "antarcticfootballleague.com")  # lowercased, dot stripped
        self.assertEqual(path, "/tickets")

    def test_rejects_invalid_ports(self):
        """urlsplit only validates the port when you read it."""
        for bad in ["https://antarcticfootballleague.com:99999/tickets",
                    "https://antarcticfootballleague.com:notaport/tickets"]:
            host, _, err = qa_gate.parse_url(bad)
            self.assertIsNotNone(err, bad)
            self.assertIsNone(host, bad)

    def test_lookalike_host_is_not_allowed(self):
        """The reason matching is exact equality and never a suffix check."""
        allowed = ["antarcticfootballleague.com"]
        self.assertTrue(qa_gate.host_allowed("antarcticfootballleague.com", allowed))
        self.assertFalse(qa_gate.host_allowed("evil-antarcticfootballleague.com", allowed))
        self.assertFalse(qa_gate.host_allowed("antarcticfootballleague.com.attacker.net", allowed))


class TestCleanBatch(unittest.TestCase):
    def test_clean_batch_produces_no_findings(self):
        report, _ = evaluate("clean_stories.json")
        self.assertEqual(report["findings"], [], report["findings"])
        self.assertEqual(report["summary"]["fail"], 0)
        self.assertEqual(report["summary"]["warning"], 0)
        self.assertEqual(report["summary"]["pages_pass"],
                         report["summary"]["pages_checked"])


class TestSummaryMatchesFindings(unittest.TestCase):
    """The headline must never contradict the list underneath it."""

    def test_story_level_failure_is_counted_in_the_summary(self):
        snapshot = {"tenant_id": "t", "stories": [
            {"story_id": "s1", "story_title": "No pages here", "pages": []}]}
        reviewer = qa_gate.RecordedSemanticReviewer(AI_CACHE)
        report = qa_gate.run(snapshot, POLICY, {"state_version": 1, "entries": {}}, reviewer)
        self.assertIn("empty_story", check_ids(report))
        # Previously 0: the summary only counted page statuses, and an empty
        # Story has no pages, so the report said "0 failures" while listing one.
        self.assertEqual(report["summary"]["fail"], 1)

    def test_every_finding_is_reflected_in_the_totals(self):
        for fixture in ["invalid_stories.json", "editorial_quality_stories.json",
                        "ambiguous_cta_stories.json"]:
            report, _ = evaluate(fixture)
            fails = sum(1 for f in report["findings"] if f["status"] == "fail")
            warns = sum(1 for f in report["findings"] if f["status"] == "warning")
            self.assertEqual(report["summary"]["fail"], fails, fixture)
            self.assertEqual(report["summary"]["warning"], warns, fixture)


class TestHumanReviewRouting(unittest.TestCase):
    """The README promises humans see only high risk or low confidence."""

    def test_low_risk_high_confidence_is_a_recommendation_not_review(self):
        report, _ = evaluate("editorial_quality_stories.json")
        generic = [f for f in report["findings"] if f["check_id"] == "generic_cta"][0]
        self.assertEqual(generic["risk"], "low")
        self.assertEqual(generic["confidence"], "high")
        self.assertFalse(generic["needs_human_review"])

    def test_routing_matches_the_documented_rule(self):
        for fixture in ["invalid_stories.json", "editorial_quality_stories.json",
                        "ambiguous_cta_stories.json"]:
            report, _ = evaluate(fixture)
            for f in report["findings"]:
                expected = f["risk"] == "high" or f["confidence"] in ("low", "medium")
                self.assertEqual(f["needs_human_review"], expected,
                                 "%s %s" % (fixture, f["check_id"]))


class TestInvalidBatch(unittest.TestCase):
    def test_catches_structural_failures(self):
        report, _ = evaluate("invalid_stories.json")
        found = check_ids(report)
        for expected in ["missing_story_id", "empty_story", "missing_asset_url",
                         "invalid_asset_url", "asset_type_mismatch",
                         "missing_page_id", "incomplete_action", "invalid_action_url"]:
            self.assertIn(expected, found)
        self.assertGreater(report["summary"]["fail"], 0)


class TestAmbiguousCta(unittest.TestCase):
    def test_rule_catches_the_brief_example(self):
        report, _ = evaluate("ambiguous_cta_stories.json")
        mismatches = [f for f in report["findings"]
                      if f["check_id"] == "cta_destination_mismatch"
                      and f["story_id"] == "story_amb_rule"]
        self.assertEqual(len(mismatches), 1)
        self.assertEqual(mismatches[0]["status"], "warning")
        self.assertEqual(mismatches[0]["confidence"], "medium")
        self.assertEqual(mismatches[0]["source"], "rule")
        self.assertEqual(mismatches[0]["risk"], "high")

    def test_model_is_only_consulted_where_rules_abstain(self):
        report, _ = evaluate("ambiguous_cta_stories.json")
        self.assertGreater(report["coverage"]["semantic_check_ai_reviewed"], 0)
        self.assertGreater(report["ai"]["cache_hits"], 0)
        # The rule case was decided without the model.
        ai_sourced = [f for f in report["findings"] if f["source"] == "ai_semantic_review"]
        self.assertTrue(all(f["story_id"] == "story_amb_ai" for f in ai_sourced))

    def test_model_mismatch_and_uncertain_both_surface(self):
        report, _ = evaluate("ambiguous_cta_stories.json")
        ai = {f["check_id"] for f in report["findings"]
              if f["source"] == "ai_semantic_review"}
        self.assertIn("cta_destination_mismatch", ai)     # Meet the squad -> /tickets
        self.assertIn("semantic_review_uncertain", ai)    # Buy tickets -> /p/48219

    def test_confident_agreement_produces_no_finding(self):
        """"Shop the new kit" -> /store/... came back match, so it must be silent."""
        report, _ = evaluate("ambiguous_cta_stories.json")
        noisy = [f for f in report["findings"] if f["page_id"] in ("page_2", "page_4")
                 and f["story_id"] == "story_amb_ai"]
        self.assertEqual(noisy, [], noisy)


class TestEditorialQuality(unittest.TestCase):
    def test_catches_editorial_problems(self):
        report, _ = evaluate("editorial_quality_stories.json")
        found = check_ids(report)
        self.assertIn("placeholder_copy", found)                 # "TODO write..."
        self.assertIn("generic_cta", found)                      # "Read more"
        self.assertIn("action_domain_not_allowed", found)        # evil-lookalike host
        self.assertIn("duplicate_asset_across_stories", found)   # same hero.jpg twice

    def test_lookalike_domain_is_flagged_not_allowed(self):
        report, _ = evaluate("editorial_quality_stories.json")
        offenders = [f for f in report["findings"]
                     if f["check_id"] == "action_domain_not_allowed"]
        self.assertEqual(len(offenders), 1)
        self.assertIn("evil-antarcticfootballleague.com", offenders[0]["reason"])
        self.assertEqual(offenders[0]["risk"], "high")


class TestCoverageIsNotNoise(unittest.TestCase):
    def test_abstention_is_counted_not_reported_as_a_defect(self):
        report, _ = evaluate("ambiguous_cta_stories.json")
        self.assertGreater(report["coverage"]["semantic_check_not_applicable"], 0)
        # "I could not classify this" must never become a review item.
        self.assertNotIn("unrecognised_cta_intent", check_ids(report))
        self.assertNotIn("unsupported_page_type", check_ids(report))


class TestStatefulReruns(unittest.TestCase):
    """The property that makes this a gate rather than a one-off script."""

    def test_second_run_skips_work_but_still_reports_everything(self):
        first, state = evaluate("ambiguous_cta_stories.json")
        self.assertEqual(first["delta"]["new"], 2)
        self.assertEqual(first["delta"]["unchanged"], 0)

        second, state = evaluate("ambiguous_cta_stories.json", state=state)
        self.assertEqual(second["delta"]["new"], 0)
        self.assertEqual(second["delta"]["changed"], 0)
        self.assertEqual(second["delta"]["unchanged"], 2)
        self.assertEqual(second["delta"]["carried_forward"], 2)

        # The important part: skipping work must NOT empty the report.
        self.assertEqual(second["summary"]["stories_checked"],
                         first["summary"]["stories_checked"])
        self.assertEqual(second["summary"]["pages_checked"],
                         first["summary"]["pages_checked"])
        self.assertEqual(len(second["findings"]), len(first["findings"]))
        self.assertTrue(any(p["source"] == "carried_forward" for p in second["pages"]))

    def test_changed_story_is_rechecked(self):
        _, state = evaluate("clean_stories.json")

        snapshot = qa_gate.load_json(os.path.join(FIXTURES, "clean_stories.json"))
        snapshot["stories"][0]["pages"][0]["action"]["cta"] = "Read more"

        reviewer = qa_gate.RecordedSemanticReviewer(AI_CACHE)
        report = qa_gate.run(snapshot, POLICY, state, reviewer)

        self.assertEqual(report["delta"]["changed"], 1)
        self.assertEqual(report["delta"]["unchanged"], 1)
        self.assertIn("generic_cta", check_ids(report))

    def test_policy_change_invalidates_carried_results(self):
        """A policy edit must re-open every Story, not silently reuse old verdicts."""
        _, state = evaluate("clean_stories.json")
        newer = dict(POLICY)
        newer["policy_version"] = "9.9.9"
        snapshot = qa_gate.load_json(os.path.join(FIXTURES, "clean_stories.json"))
        reviewer = qa_gate.RecordedSemanticReviewer(AI_CACHE)
        report = qa_gate.run(snapshot, newer, state, reviewer)
        self.assertEqual(report["delta"]["unchanged"], 0)
        self.assertEqual(report["delta"]["changed"], 2)


class TestFalsePositives(unittest.TestCase):
    """A check that punishes legitimate content is worse than no check."""

    def test_substring_collision_does_not_classify_intent(self):
        # "Facebook" contains "book", which used to classify as ticketing.
        buckets = POLICY["cta_intent_buckets"]
        self.assertIsNone(qa_gate.classify_cta("Follow us on Facebook", buckets))
        self.assertEqual(qa_gate.classify_cta("Book tickets", buckets), "ticketing")

    def test_real_titles_are_not_treated_as_draft_copy(self):
        snapshot = {"tenant_id": "t", "stories": [
            {"story_id": "s1", "story_title": "Test Match Results", "pages": []},
            {"story_id": "s2", "story_title": "Draft Kings sponsorship launch", "pages": []}]}
        reviewer = qa_gate.RecordedSemanticReviewer(AI_CACHE)
        report = qa_gate.run(snapshot, POLICY, {"state_version": 1, "entries": {}}, reviewer)
        self.assertNotIn("placeholder_copy", check_ids(report))

    def test_deliberately_reusable_assets_are_not_duplicate_warnings(self):
        shared = "https://cdn.storyteller.com/assets/sponsor/frame.jpg"
        page = {"page_id": "p", "type": "image", "asset_url": shared}
        snapshot = {"tenant_id": "t", "stories": [
            {"story_id": "s1", "story_title": "One", "pages": [dict(page)]},
            {"story_id": "s2", "story_title": "Two", "pages": [dict(page)]}]}
        reviewer = qa_gate.RecordedSemanticReviewer(AI_CACHE)
        report = qa_gate.run(snapshot, POLICY, {"state_version": 1, "entries": {}}, reviewer)
        self.assertNotIn("duplicate_asset_across_stories", check_ids(report))


class TestRobustness(unittest.TestCase):
    def test_duplicate_story_ids_are_reported(self):
        snapshot = {"tenant_id": "t", "stories": [
            {"story_id": "same", "story_title": "A", "pages": []},
            {"story_id": "same", "story_title": "B", "pages": []}]}
        reviewer = qa_gate.RecordedSemanticReviewer(AI_CACHE)
        report = qa_gate.run(snapshot, POLICY, {"state_version": 1, "entries": {}}, reviewer)
        self.assertIn("duplicate_story_id", check_ids(report))

    def test_valid_but_incomplete_state_file_does_not_crash(self):
        snapshot = qa_gate.load_json(os.path.join(FIXTURES, "clean_stories.json"))
        reviewer = qa_gate.RecordedSemanticReviewer(AI_CACHE)
        report = qa_gate.run(snapshot, POLICY, {"state_version": 1}, reviewer)  # no "entries"
        self.assertEqual(report["delta"]["new"], 2)

    def test_model_is_not_called_when_a_rule_already_flagged_the_page(self):
        snapshot = {"tenant_id": "tenant_antarctic_league_001", "stories": [
            {"story_id": "s1", "story_title": "Off domain and opaque", "pages": [
                {"page_id": "p1", "type": "image",
                 "asset_url": "https://cdn.storyteller.com/a.jpg",
                 "action": {"cta": "Buy tickets",
                            "url": "https://evil-antarcticfootballleague.com/p/48219"}}]}]}
        reviewer = qa_gate.RecordedSemanticReviewer(AI_CACHE)
        report = qa_gate.run(snapshot, POLICY, {"state_version": 1, "entries": {}}, reviewer)
        self.assertIn("action_domain_not_allowed", check_ids(report))
        # A human is already looking at this page. Paying for a model call adds nothing.
        self.assertEqual(reviewer.hits, 0)


class TestCliEndToEnd(unittest.TestCase):
    def test_cli_writes_reports_state_and_run_log(self):
        tmp = tempfile.mkdtemp()
        try:
            state = os.path.join(tmp, "state.json")
            log = os.path.join(tmp, "run_log.jsonl")
            out = os.path.join(tmp, "out")
            args = [os.path.join(ROOT, "sample_stories.json"),
                    "--policy", os.path.join(ROOT, "tenant_policy.json"),
                    "--state", state, "--ai-cache", AI_CACHE,
                    "--run-log", log, "--out", out, "--quiet"]

            self.assertEqual(qa_gate.main(args + ["--expect-delta", "new=2"]), 0)
            self.assertTrue(os.path.exists(os.path.join(out, "quality_report.json")))
            self.assertTrue(os.path.exists(os.path.join(out, "quality_report.md")))

            # Warm run: nothing new, and the report is still complete.
            self.assertEqual(qa_gate.main(args + ["--expect-delta", "new=0,unchanged=2"]), 0)
            warm = qa_gate.load_json(os.path.join(out, "quality_report.json"))
            self.assertEqual(warm["summary"]["stories_checked"], 2)
            self.assertTrue(warm["findings"])

            # One appended line per run.
            with open(log, encoding="utf-8") as fh:
                self.assertEqual(len([l for l in fh if l.strip()]), 2)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_expect_delta_failure_is_a_nonzero_exit(self):
        tmp = tempfile.mkdtemp()
        try:
            code = qa_gate.main([
                os.path.join(ROOT, "sample_stories.json"),
                "--policy", os.path.join(ROOT, "tenant_policy.json"),
                "--state", os.path.join(tmp, "s.json"),
                "--ai-cache", AI_CACHE,
                "--run-log", os.path.join(tmp, "l.jsonl"),
                "--out", os.path.join(tmp, "o"), "--quiet",
                "--expect-delta", "new=99"])
            self.assertEqual(code, 1)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
