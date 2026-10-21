"""No-network tests for the fact-first review contract and deterministic policy."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from quality_schema import ReviewDecision
from quality_schema_v2 import (QualityReviewV2, quality_review_v2_schema,
                               decide_review)


def report(**changes):
    fields = dict(facts="pass", options="pass", language="pass",
                  distractors="pass", leakage="pass", difficulty="appropriate",
                  independent_answer="A", issue_codes=[], rationale="Checked")
    fields.update(changes)
    return QualityReviewV2.model_validate(fields)


class QualityV2Tests(unittest.TestCase):
    def test_clean_review_is_accepted(self):
        self.assertEqual(decide_review(report(), "A")[0], ReviewDecision.ACCEPT)

    def test_difficulty_only_does_not_regenerate(self):
        verdict, issues = decide_review(report(difficulty="too_easy",
                          issue_codes=["difficulty_mismatch"]), "A")
        self.assertEqual(verdict, ReviewDecision.ACCEPT)
        self.assertIn("difficulty_mismatch", issues)

    def test_factual_failure_regenerates(self):
        self.assertEqual(decide_review(report(facts="fail"), "A")[0],
                         ReviewDecision.REGENERATE)

    def test_bad_distractor_regenerates(self):
        self.assertEqual(decide_review(report(distractors="fail"), "A")[0],
                         ReviewDecision.REGENERATE)

    def test_uncertain_facts_escalate(self):
        self.assertEqual(decide_review(report(facts="uncertain"), "A")[0],
                         ReviewDecision.ESCALATE)

    def test_wrong_key_escalates(self):
        self.assertEqual(decide_review(report(), "B")[0], ReviewDecision.ESCALATE)

    def test_invalid_schema_rejected(self):
        with self.assertRaises(Exception):
            report(facts="yes")

    def test_transport_schema_no_extra_properties(self):
        schema = quality_review_v2_schema()
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(set(schema["properties"]), set(schema["required"]))


if __name__ == "__main__":
    unittest.main()
