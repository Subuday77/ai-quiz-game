"""Offline regressions for key-blind factual checker (no billable calls)."""
import json
import sys
import unittest
from pathlib import Path

from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

from independent_answer_check import (
    IndependentAnswerReview, answer_review_schema, blind_payload,
    summarize_answer_review, publication_preview,
)
from question_schema import QuizQuestion
from test_openai_answer_check import CASES, payload_for_case, expected_publication


def review(outcomes, premise="valid"):
    return IndependentAnswerReview.model_validate({
        "premise": premise,
        "options": {k: {"status": v, "rationale": "Verified independently"}
                    for k, v in zip("ABCD", outcomes)},
        "rationale": "Independent option-by-option result",
    })


class IndependentAnswerTests(unittest.TestCase):
    def test_strict_schema_has_four_explicit_options(self):
        schema = answer_review_schema()
        self.assertEqual(set(schema["required"]), set(schema["properties"]))
        self.assertFalse(schema["additionalProperties"])
        options = schema["properties"]["options"]
        self.assertFalse(options["additionalProperties"])
        self.assertEqual(options["required"], list("ABCD"))
        for key in "ABCD":
            self.assertFalse(options["properties"][key]["additionalProperties"])

    def test_model_requires_all_four_and_no_additional_fields(self):
        with self.assertRaises(ValidationError):
            IndependentAnswerReview.model_validate({
                "premise": "valid",
                "options": {"A": {"status": "yes", "rationale": "Fine"}},
                "rationale": "Bad",
            })
        with self.assertRaises(ValidationError):
            IndependentAnswerReview.model_validate({
                "premise": "valid",
                "options": {"A": {"status": "yes", "rationale": "Fine"},
                            "B": {"status": "no", "rationale": "Fine"},
                            "C": {"status": "no", "rationale": "Fine"},
                            "D": {"status": "no", "rationale": "Fine"}},
                "rationale": "Fine", "generator_key": "A",
            })

    def test_blind_payload_removes_generator_key_and_level(self):
        q = QuizQuestion.model_validate({
            "difficulty": 12,
            "question": "Какой город был столицей царства Эбла?",
            "answers": {"A": "Мари", "B": "Угарит",
                        "C": "Эбла", "D": "Ниневия"},
            "correct_answer": "C",
        })
        data = json.loads(blind_payload(q, "Russian"))
        self.assertEqual(set(data), {"language", "question", "answers"})
        self.assertNotIn("correct_answer", data)
        self.assertNotIn("difficulty", data)
        self.assertNotIn("expected", data)

    def test_deterministic_classes(self):
        self.assertEqual(summarize_answer_review(review([
            "no", "yes", "no", "no"]))["outcome"], "single_correct")
        self.assertEqual(summarize_answer_review(review([
            "yes", "no", "yes", "yes"]))["outcome"], "multiple_correct")
        self.assertEqual(summarize_answer_review(review([
            "no", "no", "no", "no"]))["outcome"], "no_correct")
        self.assertEqual(summarize_answer_review(review([
            "no", "no", "no", "no"], premise="invalid"))["outcome"], "invalid_premise")
        self.assertEqual(summarize_answer_review(review([
            "no", "yes", "uncertain", "no"]))["outcome"], "uncertain")

    def test_no_false_single_when_multiple_or_uncertain(self):
        result = summarize_answer_review(review(["yes", "uncertain", "no", "no"]))
        self.assertEqual(result["outcome"], "uncertain")
        self.assertEqual(result["supported_choices"], ["A"])
        self.assertEqual(result["uncertain_choices"], ["B"])

    def test_hypothetical_gate_rejects_bad_key_multiple_and_literal_leak(self):
        good = review(["no", "yes", "no", "no"])
        self.assertFalse(publication_preview(good, "B", False)["would_block"])
        self.assertEqual(publication_preview(good, "A", False)["reasons"],
                         ["generator_key_mismatch"])
        self.assertEqual(publication_preview(good, "B", True)["reasons"],
                         ["literal_answer_leak"])
        several = review(["yes", "no", "yes", "yes"])
        self.assertEqual(publication_preview(several, "A", False)["reasons"],
                         ["multiple_correct"])
        missing = review(["no", "no", "no", "no"])
        self.assertEqual(publication_preview(missing, "A", False)["reasons"],
                         ["no_correct"])
        disputed = review(["no", "yes", "uncertain", "no"])
        self.assertEqual(publication_preview(disputed, "B", False)["reasons"],
                         ["uncertain"])
        with self.assertRaises(ValueError):
            publication_preview(good, "X", False)

    def test_benchmark_expected_release_distinguishes_bad_key_and_leak(self):
        for case_id in ("saturn_wrong_key", "enclave_three_correct",
                        "mrs_dalloway_direct_leak", "hanno_periplus",
                        "ebla_direct_leak", "empty_answer_set",
                        "incorrect_nickname_premise"):
            self.assertEqual(expected_publication(case_id), "block")
        self.assertEqual(expected_publication("piano"), "allow")
        self.assertEqual(expected_publication("mussorgsky_versions"), "review")

    def test_benchmark_contains_both_good_and_bad_questions(self):
        self.assertGreaterEqual(len(CASES), 12)
        self.assertGreaterEqual(sum(case["expected"] == "single_correct" for case in CASES), 6)
        self.assertGreaterEqual(sum(case["expected"] != "single_correct" for case in CASES), 3)
        for case in CASES:
            with self.subTest(case=case["id"]):
                q, payload = payload_for_case(case)
                parsed = json.loads(payload)
                self.assertNotIn("correct_answer", parsed)
                self.assertNotIn("key", parsed)
                self.assertNotIn("expected", parsed)
                self.assertEqual(len(parsed["answers"]), 4)
                self.assertEqual(q.correct_answer, case["key"])

    def test_wrong_generator_key_stays_hidden(self):
        case = next(c for c in CASES if c["id"] == "saturn_wrong_key")
        q, raw = payload_for_case(case)
        self.assertNotEqual(q.correct_answer, case["accepted"])
        self.assertEqual(json.loads(raw)["answers"]["C"], "Сатурн")


if __name__ == "__main__":
    unittest.main()
