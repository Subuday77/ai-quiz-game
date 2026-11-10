"""Budget and browser timeout contract for late-game question recovery."""
import os
import unittest
from unittest.mock import patch

from generation_policy import GenerationPolicy, client_settings


class LateQuestionBudgetTests(unittest.TestCase):
    def test_only_q13_through_q16_get_extended_budget(self):
        with patch.dict(os.environ, {
            "AI_GENERATION_BUDGET_SECONDS": "30",
            "AI_LATE_GAME_BUDGET_SECONDS": "60",
        }):
            self.assertEqual(GenerationPolicy.from_env().budget_seconds, 30)
            self.assertEqual(GenerationPolicy.from_env(question_number=1).budget_seconds, 30)
            self.assertEqual(GenerationPolicy.from_env(question_number=12).budget_seconds, 30)
            for number in (13, 14, 15, 16):
                with self.subTest(question=number):
                    self.assertEqual(
                        GenerationPolicy.from_env(question_number=number).budget_seconds,
                        60,
                    )

    def test_custom_baseline_is_never_shortened(self):
        with patch.dict(os.environ, {
            "AI_GENERATION_BUDGET_SECONDS": "90",
            "AI_LATE_GAME_BUDGET_SECONDS": "60",
        }):
            self.assertEqual(
                GenerationPolicy.from_env(question_number=14).budget_seconds, 90,
            )

    def test_invalid_question_and_budget_are_rejected(self):
        with patch.dict(os.environ, {
            "AI_GENERATION_BUDGET_SECONDS": "30",
            "AI_LATE_GAME_BUDGET_SECONDS": "60",
        }):
            for number in (0, 17):
                with self.assertRaises(ValueError):
                    GenerationPolicy.from_env(question_number=number)
        with patch.dict(os.environ, {"AI_LATE_GAME_BUDGET_SECONDS": "0"}):
            with self.assertRaises(ValueError):
                GenerationPolicy.from_env(question_number=14)

    def test_frontend_receives_longer_late_question_timeout(self):
        with patch.dict(os.environ, {
            "QUIZ_LONG_WAIT_WARNING_SECONDS": "15",
            "QUIZ_HTTP_TIMEOUT_SECONDS": "45",
            "QUIZ_LATE_QUESTION_TIMEOUT_SECONDS": "140",
        }):
            self.assertEqual(client_settings(), {
                "long_wait_warning_ms": 15000,
                "request_timeout_ms": 45000,
                "late_question_timeout_ms": 140000,
            })

    def test_rejects_shorter_late_timeout(self):
        with patch.dict(os.environ, {
            "QUIZ_HTTP_TIMEOUT_SECONDS": "45",
            "QUIZ_LATE_QUESTION_TIMEOUT_SECONDS": "25",
        }):
            with self.assertRaises(ValueError):
                client_settings()


if __name__ == "__main__":
    unittest.main()
