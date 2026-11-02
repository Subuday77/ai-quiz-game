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
from test_openai_answer_check import (
    CASES, payload_for_case, expected_publication, run_case, select_cases,
)


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


    def test_playtest_cases_include_all_four_answer_patterns(self):
        by_id = {case["id"]: case for case in CASES}
        self.assertEqual(len(by_id), len(CASES), "Case IDs must be unique")
        expected = {
            "jam_only_number_one": ("invalid_premise", "", "block"),
            "jam_four_number_ones": ("multiple_correct", "ABCD", "block"),
            "beatles_1967_drums": ("invalid_premise", "", "block"),
            "kate_bush_2022_debut": ("invalid_premise", "", "block"),
            "ginkgo_motile_sperm": ("single_correct", "A", "allow"),
            "vice_city_title_leak": ("single_correct", "B", "block"),
            "fenrir_odin": ("single_correct", "C", "allow"),
            "riemann_trivial_zeros": ("single_correct", "A", "allow"),
        }
        for case_id, (outcome, choices, publication) in expected.items():
            with self.subTest(case=case_id):
                case = by_id[case_id]
                self.assertEqual((case["expected"], case["accepted"]),
                                 (outcome, choices))
                self.assertEqual(expected_publication(case_id), publication)
                q, blind = payload_for_case(case)
                self.assertEqual(q.question, case["stem"])
                self.assertEqual(tuple(q.answers[k] for k in "ABCD"),
                                 case["answers"])
                self.assertEqual(set(json.loads(blind)), {"language", "question", "answers"})
                self.assertNotIn("correct_answer", blind)

    def test_gold_adjudications_are_classified_without_relying_on_model_key(self):
        fixtures = [
            ("jam_only_number_one", ("no", "no", "no", "no"), "invalid", False),
            ("jam_four_number_ones", ("yes", "yes", "yes", "yes"), "valid", False),
            ("beatles_1967_drums", ("no", "no", "no", "no"), "invalid", False),
            ("kate_bush_2022_debut", ("no", "no", "no", "no"), "invalid", False),
            ("ginkgo_motile_sperm", ("yes", "no", "no", "no"), "valid", False),
            ("vice_city_title_leak", ("no", "yes", "no", "no"), "valid", True),
            ("fenrir_odin", ("no", "no", "yes", "no"), "valid", False),
            ("riemann_trivial_zeros", ("yes", "no", "no", "no"), "valid", False),
        ]
        by_id = {case["id"]: case for case in CASES}
        for case_id, statuses, premise, leaked in fixtures:
            with self.subTest(case=case_id):
                case = by_id[case_id]
                evaluation = review(statuses, premise)
                result = summarize_answer_review(evaluation)
                self.assertEqual(result["outcome"], case["expected"])
                self.assertEqual("".join(result["supported_choices"]),
                                 case["accepted"])
                preview = publication_preview(evaluation, case["key"], leaked)
                self.assertEqual(preview["would_block"],
                                 expected_publication(case_id) == "block")

    def test_case_selection_limits_cost_and_fails_on_unknown_names(self):
        chosen = select_cases(ids=["vice_city_title_leak", "jam_only_number_one"])
        self.assertEqual([case["id"] for case in chosen],
                         ["jam_only_number_one", "vice_city_title_leak"])
        self.assertEqual(len(select_cases(limit=2)), 2)
        self.assertEqual(len(select_cases()), len(CASES))
        for bad in (0, -1, len(CASES) + 1):
            with self.subTest(limit=bad), self.assertRaises(ValueError):
                select_cases(limit=bad)
        with self.assertRaisesRegex(ValueError, "Unknown cases"):
            select_cases(ids=["case-that-does-not-exist"])

    def test_live_runner_protocol_is_key_blind_and_emits_all_options_offline(self):
        from unittest.mock import Mock, patch

        case = next(c for c in CASES if c["id"] == "jam_four_number_ones")
        independent = review(("yes", "yes", "yes", "yes"))
        fake_response = Mock()
        fake_response.status = "completed"
        fake_response.output_text = independent.model_dump_json()
        fake_response.usage = None
        client = Mock()
        client.responses.create.return_value = fake_response
        with patch("test_openai_answer_check.exact_answer_leak", return_value=False):
            actual = run_case(client, case)
        request = client.responses.create.call_args.kwargs
        sent = json.loads(request["input"])
        self.assertEqual(set(sent), {"language", "question", "answers"})
        self.assertNotIn("correct_answer", sent)
        self.assertNotIn("difficulty", sent)
        self.assertNotIn("expected", sent)
        self.assertEqual(request["text"]["format"]["schema"],
                         answer_review_schema())
        self.assertEqual(actual["observed"], "multiple_correct")
        self.assertEqual(actual["observed_choices"], "ABCD")
        self.assertEqual({k: row["status"] for k, row in actual["per_option"].items()},
                         dict.fromkeys("ABCD", "yes"))
        self.assertEqual(actual["release_preview"], "block")
        self.assertEqual(actual["expected_release"], "block")
        self.assertEqual(actual["choices_match"], True)

    def test_wrong_generator_key_stays_hidden(self):
        case = next(c for c in CASES if c["id"] == "saturn_wrong_key")
        q, raw = payload_for_case(case)
        self.assertNotEqual(q.correct_answer, case["accepted"])
        self.assertEqual(json.loads(raw)["answers"]["C"], "Сатурн")


if __name__ == "__main__":
    unittest.main()
