"""Two-stage critic: offline schema and deterministic Python scorer tests."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from agents.critic_two_stage import TWO_STAGE_PROMPT
from quality_two_stage import (TwoStageReview, distractor_penalty,
                               score_two_stage, two_stage_schema)


def make_review(*, correct="A", options=None, iconic=False, **changes):
    if options is None:
        options = [
            ("B", "obvious_wrong", "basic"),
            ("C", "obvious_wrong", "basic"),
            ("D", "obvious_wrong", "basic"),
        ]
    fields = {
        "facts": "pass", "options": "pass", "language": "pass",
        "leakage": "pass", "difficulty": "appropriate",
        "independent_answer": correct, "issue_codes": [],
        "wrong_options": [
            {"choice": name, "plausibility": kind,
             "separation_knowledge": needed, "reason": "Specific difference"}
            for name, kind, needed in options
        ],
        "iconic_key_association": iconic,
        "knowledge_penalty": 0, "suitability_penalty": 0,
        "wording_penalty": 0, "rationale": "Independent factual review",
    }
    fields.update(changes)
    return TwoStageReview.model_validate(fields)


class TwoStageScoreTests(unittest.TestCase):
    def test_trivial_piano_question_has_no_penalty(self):
        result = score_two_stage(make_review(), 0, "A")
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["critical"], "none")

    def test_hola_close_wrong_answer_is_too_hard_at_level_one(self):
        review = make_review(options=[
            ("B", "near_miss", "specialist"),
            ("C", "obvious_wrong", "basic"),
            ("D", "obvious_wrong", "basic"),
        ])
        self.assertEqual(distractor_penalty(review, 1, "A"), 2)

    def test_hola_distant_options_no_penalty(self):
        self.assertEqual(distractor_penalty(make_review(), 1, "A"), 0)

    def test_gutenberg_throwaways_penalized_by_difficulty(self):
        review = make_review()
        self.assertEqual(distractor_penalty(review, 4, "A"), 2)
        self.assertEqual(distractor_penalty(review, 8, "A"), 3)

    def test_iconic_ring_association_changes_with_level(self):
        review = make_review(iconic=True, options=[
            ("B", "plausible_wrong", "general"),
            ("C", "plausible_wrong", "general"),
            ("D", "plausible_wrong", "general"),
        ])
        self.assertEqual(distractor_penalty(review, 1, "A"), 0)
        self.assertEqual(distractor_penalty(review, 5, "A"), 2)

    def test_one_real_near_competitor_protects_high_level_question(self):
        review = make_review(iconic=True, options=[
            ("B", "near_miss", "specialist"),
            ("C", "plausible_wrong", "general"),
            ("D", "plausible_wrong", "general"),
        ])
        self.assertEqual(distractor_penalty(review, 8, "A"), 1)

    def test_no_double_charge_for_choice_based_difficulty(self):
        review = make_review(knowledge_penalty=0, iconic=True)
        self.assertEqual(score_two_stage(review, 5, "A")["penalties"]["knowledge"], 0)

    def test_actual_leakage_remains_critical_even_with_zero_points(self):
        review = make_review(leakage="fail", issue_codes=["answer_leak"],
                             options=[
                                 ("B", "plausible_wrong", "general"),
                                 ("C", "plausible_wrong", "general"),
                                 ("D", "plausible_wrong", "general"),
                             ])
        self.assertEqual(score_two_stage(review, 3, "A")["critical"], "regenerate")

    def test_invalid_wrong_choice_and_duplicates_fail_closed(self):
        review = make_review()
        with self.assertRaises(ValueError):
            distractor_penalty(review, 7, "B")
        data = review.model_dump()
        data["wrong_options"][1]["choice"] = "B"
        with self.assertRaises(ValueError):
            TwoStageReview.model_validate(data)

    def test_schema_strict_and_no_score_field(self):
        schema = two_stage_schema()
        self.assertEqual(set(schema["required"]), set(schema["properties"]))
        self.assertFalse(schema["additionalProperties"])
        self.assertFalse(schema["properties"]["wrong_options"]["items"]["additionalProperties"])
        self.assertNotIn("distractor_penalty", schema["properties"])
        self.assertNotIn("decision", schema["properties"])

    def test_model_returns_evidence_not_scoring(self):
        for marker in ("plausibility", "separation_knowledge",
                       "iconic_key_association", "Do NOT provide",
                       "quotation, greeting", "Do NOT anticipate"):
            self.assertIn(marker, TWO_STAGE_PROMPT)

    def test_existing_v3_untouched(self):
        from agents.critic_v3 import QUALITY_REVIEW_V3_PROMPT
        self.assertIn("distractor_penalty: integer 0..3", QUALITY_REVIEW_V3_PROMPT)
        self.assertNotIn("TWO-STAGE OUTPUT OVERRIDE", QUALITY_REVIEW_V3_PROMPT)


if __name__ == "__main__":
    unittest.main()
