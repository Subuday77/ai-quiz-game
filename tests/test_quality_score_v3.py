"""Offline score policy and strict-schema tests for the audit-only v3 critic."""
import sys
import unittest
from pathlib import Path

from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from output_contracts import OutputContract, transport_contract
from quality_schema_v3 import QualityReviewV3, audit_score, quality_review_v3_schema
from agents.critic_v3 import QUALITY_REVIEW_V3_PROMPT


def report(**changes):
    fields = dict(facts="pass", options="pass", language="pass", leakage="pass",
                  difficulty="appropriate", independent_answer="B",
                  issue_codes=[], distractor_penalty=0, knowledge_penalty=0,
                  suitability_penalty=0, wording_penalty=0, rationale="Clear")
    fields.update(changes)
    return QualityReviewV3.model_validate(fields)


class ScoreV3Tests(unittest.TestCase):
    def test_zero_penalty_question(self):
        result = audit_score(report(), "B")
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["critical"], "none")
        self.assertFalse(any(result["threshold_preview"].values()))

    def test_weak_distractors_at_high_level(self):
        result = audit_score(report(distractor_penalty=3), "B")
        self.assertEqual(result["total"], 3)
        self.assertTrue(result["threshold_preview"]["3"])
        self.assertFalse(result["threshold_preview"]["4"])
        self.assertEqual(result["critical"], "none")

    def test_scores_sum_independently(self):
        result = audit_score(report(distractor_penalty=1,
                    knowledge_penalty=2, suitability_penalty=1,
                    wording_penalty=1), "B")
        self.assertEqual(result["total"], 5)
        self.assertEqual(result["penalties"]["wording"], 1)

    def test_bad_fact_is_critical_even_with_zero_penalty(self):
        self.assertEqual(audit_score(report(facts="fail",
                    issue_codes=["factual_error"]), "B")["critical"], "regenerate")

    def test_answer_leak_is_critical_even_with_zero_penalty(self):
        self.assertEqual(audit_score(report(leakage="fail",
                    issue_codes=["answer_leak"]), "B")["critical"], "regenerate")

    def test_wrong_key_requires_escalation(self):
        self.assertEqual(audit_score(report(), "A")["critical"], "escalate")

    def test_uncertain_facts_require_escalation(self):
        self.assertEqual(audit_score(report(facts="uncertain"), "B")["critical"], "escalate")

    def test_range_validation_and_no_extra_fields(self):
        for field, bad in (("distractor_penalty", 4), ("knowledge_penalty", -1),
                           ("suitability_penalty", 3), ("wording_penalty", 2)):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                report(**{field: bad})
        with self.assertRaises(ValidationError):
            report(unexpected_field=9)

    def test_strict_transport_contract(self):
        name, schema = transport_contract(OutputContract.QUALITY_REVIEW_V3)
        self.assertEqual(name, "quality_review_v3")
        self.assertEqual(set(schema["required"]), set(schema["properties"]))
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["properties"]["distractor_penalty"]["enum"],
                         [0, 1, 2, 3])
        self.assertNotIn("distractor_penalty",
                         transport_contract(OutputContract.QUALITY_REVIEW_V2)[1]["properties"])

    def test_prompt_preserves_v2_and_shared_scale(self):
        self.assertIn("REASONING VERSUS LEAKAGE", QUALITY_REVIEW_V3_PROMPT)
        self.assertIn("Shared 0–15 quiz difficulty scale", QUALITY_REVIEW_V3_PROMPT)
        self.assertIn("At levels 0–3", QUALITY_REVIEW_V3_PROMPT)
        self.assertIn("Do NOT charge a knowledge penalty a second time",
                      QUALITY_REVIEW_V3_PROMPT)


if __name__ == "__main__":
    unittest.main()
