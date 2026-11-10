"""No-network v3 quality audit integration tests."""
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
from quality_checker import check_quality
from quality_errors import QualityConfigurationError
from quality_schema import QualityMode
from output_contracts import OutputContract


QUESTION = QuizQuestion.model_validate({
    "difficulty": 8,
    "question": "Какое наземное животное из перечисленных в среднем самое тяжёлое?",
    "answers": {
        "A": "Африканский саванный слон", "B": "Белый носорог",
        "C": "Бегемот", "D": "Индийский слон",
    },
    "correct_answer": "A",
})


class FakeClient:
    calls = []
    response = None

    def __init__(self, provider, *, model_id=None, structured_output_mode=None, role=None):
        self.calls.append(("setup", provider, model_id, structured_output_mode, role))
        self.responses = self

    def create(self, **kwargs):
        self.calls.append(("request", kwargs))
        return ProviderResult(json.dumps(self.response), Completion.COMPLETE, "completed")


class AuditV3IntegrationTests(unittest.TestCase):
    def setUp(self):
        FakeClient.calls = []
        FakeClient.response = {
            "facts": "pass", "options": "pass", "language": "pass",
            "leakage": "pass", "difficulty": "appropriate",
            "independent_answer": "A", "issue_codes": [],
            "distractor_penalty": 3, "knowledge_penalty": 0,
            "suitability_penalty": 0, "wording_penalty": 0,
            "rationale": "Unrealistic distractors for a challenging level.",
        }
        self.env = patch.dict(os.environ, {
            "AI_QUALITY_CONTRACT": "v3",
            "AI_QUALITY_PROVIDER": "openai",
            "AI_QUALITY_MODEL_ID": "gpt-5.4-mini",
            "AI_QUALITY_STRUCTURED_OUTPUT_MODE": "strict",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.mock_client = patch("quality_checker.LLMClient", FakeClient)
        self.mock_client.start()
        self.addCleanup(self.mock_client.stop)
        self.events = []
        self.mock_event = patch("quality_checker.event",
                                side_effect=lambda event_name, **kw: self.events.append((event_name, kw)))
        self.mock_event.start()
        self.addCleanup(self.mock_event.stop)

    def check(self, mode):
        check_quality(QUESTION, 8, "Russian", mode,
                      deadline=perf_counter() + 25, timeout_seconds=12)

    def test_score_logged_without_rejection(self):
        self.check(QualityMode.AUDIT)
        score = [data for name, data in self.events if name == "quality_review_score"]
        self.assertEqual(len(score), 1)
        self.assertEqual(score[0]["score"], 3)
        self.assertTrue(score[0]["threshold_preview"]["3"])
        self.assertFalse(score[0]["threshold_preview"]["4"])
        self.assertEqual(score[0]["critical"], "none")
        self.assertEqual(FakeClient.calls[1][1]["output_contract"],
                         OutputContract.QUALITY_REVIEW_V3)
        self.assertEqual(FakeClient.calls[1][1]["reasoning"], {"effort": "low"})

    def test_critical_recommendation_is_audit_only(self):
        FakeClient.response.update(facts="fail", issue_codes=["factual_error"])
        self.check(QualityMode.AUDIT)
        score = [data for name, data in self.events if name == "quality_review_score"][0]
        self.assertEqual(score["critical"], "regenerate")
        self.assertFalse(any(name == "quality_question_rejected" for name, _ in self.events))

    def test_v3_refuses_enforce_before_any_provider_call(self):
        with self.assertRaises(QualityConfigurationError):
            self.check(QualityMode.ENFORCE)
        self.assertEqual(FakeClient.calls, [])

    def test_off_mode_remains_noop(self):
        self.check(QualityMode.OFF)
        self.assertEqual(FakeClient.calls, [])


if __name__ == "__main__":
    unittest.main()
