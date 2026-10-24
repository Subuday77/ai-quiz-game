"""Offline integration smoke tests for the configured generator/reviewer pipeline."""
import json
import os
import sys
import unittest
from pathlib import Path
from time import perf_counter
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from provider_result import ProviderResult, Completion
from question_schema import QuizQuestion
from quality_schema import QualityMode
from quality_checker import check_quality
from quality_errors import QualityRegenerationError


QUESTION = QuizQuestion.model_validate({
    "difficulty": 8,
    "question": "Какое из этих чисел равно 2 + 2?",
    "answers": {"A": "3", "B": "4", "C": "5", "D": "6"},
    "correct_answer": "B",
})


class FakeClient:
    requests = []
    response = None

    def __init__(self, provider, *, model_id=None, structured_output_mode=None, role=None):
        self.requests.append({"provider": provider, "model": model_id,
                              "structured": structured_output_mode, "role": role})
        self.responses = self

    def create(self, **kwargs):
        self.requests.append(kwargs)
        return ProviderResult(json.dumps(self.response), Completion.COMPLETE, "completed")


class CriticV2IntegrationTests(unittest.TestCase):
    def setUp(self):
        FakeClient.requests = []
        FakeClient.response = {
            "facts": "pass", "options": "pass", "language": "pass",
            "distractors": "pass", "leakage": "pass", "difficulty": "too_easy",
            "independent_answer": "B", "issue_codes": ["difficulty_mismatch"],
            "rationale": "Correct question; easier than requested.",
        }
        self.environment = patch.dict(os.environ, {
            "AI_QUALITY_PROVIDER": "openai",
            "AI_QUALITY_MODEL_ID": "gpt-5.4-mini",
            "AI_QUALITY_STRUCTURED_OUTPUT_MODE": "strict",
            "AI_QUALITY_CONTRACT": "v2",
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.client_patch = patch("quality_checker.LLMClient", FakeClient)
        self.client_patch.start()
        self.addCleanup(self.client_patch.stop)

    def check(self, mode):
        check_quality(QUESTION, 8, "Russian", mode,
                      deadline=perf_counter() + 25, timeout_seconds=12)

    def test_critic_is_openai_mini_v2_strict_and_low_reasoning(self):
        self.check(QualityMode.AUDIT)
        setup, request = FakeClient.requests
        self.assertEqual(setup, {"provider": "openai", "model": "gpt-5.4-mini",
                                 "structured": "strict", "role": "reviewer"})
        self.assertEqual(request["output_contract"].value, "quality_review_v2")
        self.assertEqual(request["reasoning"], {"effort": "low"})
        self.assertGreaterEqual(request["max_output_tokens"], 800)

    def test_difficulty_only_does_not_reject_in_enforce(self):
        self.check(QualityMode.ENFORCE)

    def test_factual_failure_is_logged_only_in_audit(self):
        FakeClient.response.update(facts="fail", issue_codes=["factual_error"])
        self.check(QualityMode.AUDIT)

    def test_factual_failure_rejects_in_enforce(self):
        FakeClient.response.update(facts="fail", issue_codes=["factual_error"])
        with self.assertRaises(QualityRegenerationError):
            self.check(QualityMode.ENFORCE)


if __name__ == "__main__":
    unittest.main()
