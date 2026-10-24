"""Offline regressions for Critic v3 editorial instructions and blind calibration data."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from agents.critic_v3 import QUALITY_REVIEW_V3_PROMPT
from quality_schema_v3 import QualityReviewV3, audit_score
from test_openai_critic_v3_calibration import (CASES, REAL_GAME_CASES,
                                               candidate_payload, question_from_case)


class V3EditorialCalibrationTests(unittest.TestCase):
    def test_v3_keeps_critical_review_and_never_enforces_score(self):
        for marker in ("CRITICAL CHECKS", "MULTIPLE", "critical",
                       "AUDIT ONLY", "NEVER make a publication"):
            if marker == "MULTIPLE":
                self.assertIn("multiple correct choices", QUALITY_REVIEW_V3_PROMPT)
            elif marker == "NEVER make a publication":
                self.assertIn("NEVER make a publication", QUALITY_REVIEW_V3_PROMPT)
            else:
                self.assertIn(marker, QUALITY_REVIEW_V3_PROMPT)

    def test_leakage_is_not_quote_recognition(self):
        for marker in ("Recognition of a KNOWN PHRASE", "is NOT answer leakage",
                       "OUTSIDE knowledge", "EXACT wording-to-option shortcut",
                       "Do not use answer_leak to express high familiarity"):
            self.assertIn(marker, QUALITY_REVIEW_V3_PROMPT)

    def test_distractors_are_bidirectional_at_all_levels(self):
        for marker in ("TOO WEAK", "TOO STRONG", "At levels 0–3",
                       "A single highly confusable wrong option",
                       "overwhelmingly famous", "knowledge_penalty must be 0"):
            self.assertIn(marker, QUALITY_REVIEW_V3_PROMPT)

    def test_fixed_blind_cases_and_pairs(self):
        cases = {case["id"]: case for case in CASES}
        self.assertEqual(len(cases), 12)
        self.assertEqual(cases["hola_near_easy"]["stem"], cases["hola_far_easy"]["stem"])
        self.assertEqual(cases["saturn_easy"]["stem"], cases["saturn_famous_medium"]["stem"])
        self.assertEqual(cases["gutenberg_medium"]["stem"], cases["gutenberg_high"]["stem"])
        self.assertEqual(cases["gutenberg_medium"]["answers"], cases["gutenberg_high"]["answers"])
        for case in CASES:
            with self.subTest(case=case["id"]):
                question = question_from_case(case)
                self.assertEqual(len(question.answers), 4)
                self.assertEqual(question.correct_answer, case["key"])
                self.assertTrue(0 <= case["min_d"] <= case["max_d"] <= 3)
                self.assertIn(case["leak"], ("pass", "fail"))

    def test_real_game_replay_matches_eight_screenshots(self):
        self.assertEqual(len(REAL_GAME_CASES), 8)
        self.assertEqual([case["difficulty"] for case in REAL_GAME_CASES],
                         list(range(8)))
        cases = {case["id"]: case for case in REAL_GAME_CASES}
        self.assertEqual(cases["game_02_hola"]["key"], "C")
        self.assertEqual(cases["game_04_gearbox"]["key"], "B")
        self.assertEqual(cases["game_07_xfiles"]["answers"][1], "The X-Files")
        self.assertEqual(cases["game_08_hanami"]["answers"][1], "Ханами")
        self.assertTrue(all(case["leak"] == "pass" for case in REAL_GAME_CASES))
        for case in REAL_GAME_CASES:
            with self.subTest(case=case["id"]):
                question = question_from_case(case)
                self.assertEqual(len(question.answers), 4)
                self.assertIn(case["key"], "ABCD")

    def test_expected_labels_are_never_sent_to_reviewer(self):
        for case in CASES + REAL_GAME_CASES:
            with self.subTest(case=case["id"]):
                payload = candidate_payload(question_from_case(case))
                self.assertEqual(set(payload), {
                    "requested_difficulty", "language", "candidate"
                })
                candidate = payload["candidate"]
                self.assertNotIn("min_d", candidate)
                self.assertNotIn("max_d", candidate)
                self.assertNotIn("leak", candidate)
                self.assertNotIn("id", candidate)
                self.assertNotIn("category", candidate)

    def test_hard_error_never_demoted_by_penalties(self):
        report = QualityReviewV3.model_validate({
            "facts": "pass", "options": "pass", "language": "pass",
            "leakage": "fail", "difficulty": "appropriate",
            "independent_answer": "B", "issue_codes": ["answer_leak"],
            "distractor_penalty": 0, "knowledge_penalty": 0,
            "suitability_penalty": 0, "wording_penalty": 0,
            "rationale": "Named direct clue.",
        })
        self.assertEqual(audit_score(report, "B")["critical"], "regenerate")
        self.assertEqual(audit_score(report, "B")["total"], 0)


if __name__ == "__main__":
    unittest.main()
