"""Frozen 2026-11-02 game failures: no provider or database needed.

These regressions guarantee fixture integrity, safe deterministic gates and
critic instructions. They do NOT claim LLM accuracy; use the paid replay
script for that independent measurement.
"""
import json
import unittest
from pathlib import Path

from local_leak_filter import exact_answer_leak
from question_generator import TAGGING_GENERATOR_INSTRUCTIONS
from tagging.contract import TaggedQuestion
from tagging.review import TAGGED_REVIEW_PROMPT

CASES = {
    item["id"]: item
    for item in json.loads(
        (Path(__file__).parent / "fixtures" / "nov2_quality_regressions.json")
        .read_text(encoding="utf-8")
    )["cases"]
}


class NovemberGameRegressionTests(unittest.TestCase):
    def test_exact_three_logged_candidates_validate_without_mutation(self):
        self.assertEqual(set(CASES), {
            "cannes_1976_false_shared_winner",
            "transsib_obvious_western_terminus",
            "xerox_alto_brand_leak",
        })
        for case in CASES.values():
            with self.subTest(case=case["id"]):
                item = TaggedQuestion.model_validate(case["question"])
                self.assertEqual(set(item.answers), set("ABCD"))
                self.assertEqual(item.model_dump(), case["question"])

    def test_cannes_named_co_winner_is_a_distinct_false_claim(self):
        # Official 1975/1976 Cannes award records (not a model verdict):
        # https://www.festival-cannes.com/en/retrospective/1975/awards/
        # https://www.festival-cannes.com/en/retrospective/1976/awards/
        q = CASES["cannes_1976_false_shared_winner"]
        self.assertEqual(q["expected"]["premise"], "invalid")
        self.assertIn("1976", q["question"]["question"])
        self.assertIn("«Хроника лет огня»", q["question"]["question"])
        self.assertEqual(q["question"]["answers"]["A"], "«Таксист»")
        self.assertIn("shared", TAGGED_REVIEW_PROMPT)
        self.assertIn("1975", TAGGED_REVIEW_PROMPT)
        self.assertIn("1976", TAGGED_REVIEW_PROMPT)
        self.assertIn("extra historical assertion", TAGGING_GENERATOR_INSTRUCTIONS)

    def test_transsib_distractors_require_geographic_elimination_audit(self):
        q = CASES["transsib_obvious_western_terminus"]
        self.assertEqual(q["question"]["answers"], {
            "A": "Владивосток", "B": "Новосибирск",
            "C": "Москва", "D": "Екатеринбург",
        })
        self.assertEqual(q["expected"]["minimum_distractor_penalty"], 2)
        self.assertIn("western/eastern endpoint", TAGGED_REVIEW_PROMPT)
        self.assertIn("distractor_penalty 2", TAGGED_REVIEW_PROMPT)
        self.assertIn("direction/terminus question", TAGGING_GENERATOR_INSTRUCTIONS)
        self.assertFalse(exact_answer_leak(TaggedQuestion.model_validate(q["question"])))

    def test_live_replay_evaluator_compares_expected_fields_without_model_calls(self):
        from replay_tagged_quality_nov2 import verdict_matches_expected
        good_cannes = {"premise": "invalid", "leakage": "pass",
                       "distractor_penalty": 0}
        wrong_cannes = {"premise": "valid", "leakage": "pass",
                        "distractor_penalty": 0}
        self.assertTrue(verdict_matches_expected(
            CASES["cannes_1976_false_shared_winner"]["expected"], good_cannes
        ))
        self.assertFalse(verdict_matches_expected(
            CASES["cannes_1976_false_shared_winner"]["expected"], wrong_cannes
        ))
        self.assertFalse(verdict_matches_expected(
            CASES["xerox_alto_brand_leak"]["expected"],
            {"premise": "valid", "leakage": "pass"},
        ))
        self.assertTrue(verdict_matches_expected(
            CASES["xerox_alto_brand_leak"]["expected"],
            {"premise": "valid", "leakage": "fail"},
        ))
        self.assertFalse(verdict_matches_expected(
            CASES["transsib_obvious_western_terminus"]["expected"],
            {"premise": "valid", "distractor_penalty": 0},
        ))
        self.assertTrue(verdict_matches_expected(
            CASES["transsib_obvious_western_terminus"]["expected"],
            {"premise": "valid", "distractor_penalty": 2},
        ))

    def test_xerox_is_locally_detected_without_new_model_gates(self):
        q = CASES["xerox_alto_brand_leak"]
        item = TaggedQuestion.model_validate(q["question"])
        self.assertTrue(exact_answer_leak(item))
        self.assertEqual(q["expected"]["leakage"], "fail")
        self.assertIn("Xerox Alto", TAGGED_REVIEW_PROMPT)
        self.assertIn("Xerox PARC", TAGGED_REVIEW_PROMPT)


if __name__ == "__main__":
    unittest.main()
