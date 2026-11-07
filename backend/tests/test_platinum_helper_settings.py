"""Offline checks: one root .env.example and real Platinum Helper overrides."""
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from dotenv import dotenv_values

from platinum_helper.settings import HelperSettings
from platinum_helper.selection import best_visible_wrong, cautious_threshold
from platinum_helper.responder import talk


class StubClient:
    def __init__(self):
        self.requests = []
        self.options = []
        self.responses = self

    def with_options(self, **kwargs):
        self.options.append(kwargs)
        return self

    def create(self, **kwargs):
        from types import SimpleNamespace
        self.requests.append(kwargs)
        return SimpleNamespace(status="completed", output_text="KEY: A\nSPEECH: I pick 395.")


class HelperEnvironmentTests(unittest.TestCase):
    def test_root_example_is_complete_and_excludes_obsolete_settings(self):
        path = Path(__file__).resolve().parents[2] / ".env.example"
        values = dotenv_values(path)
        required = (
            "OPENAI_API_KEY", "DATABASE_URL", "LLM_PROVIDER", "OPENAI_MODEL_ID",
            "AI_QUALITY_MODE", "AI_TAG_DEDUP_ENABLED",
            "QUIZ_QUALITY_SKILL_MODE", "QUIZ_QUALITY_ARBITER_MODE",
            "QUIZ_QUALITY_ARBITER_MODEL_ID", "QUIZ_HELPER_MODEL_ID",
            "QUIZ_HELPER_REASONING_EFFORT", "QUIZ_HELPER_MAX_OUTPUT_TOKENS",
            "QUIZ_HELPER_TIMEOUT_SECONDS", "QUIZ_HELPER_MAX_ATTEMPTS",
            "QUIZ_HELPER_PLAUSIBILITY_LEVEL_0",
            "QUIZ_HELPER_PLAUSIBILITY_LEVELS_1_3",
            "QUIZ_HELPER_PLAUSIBILITY_LEVELS_4_15",
        )
        for name in required:
            with self.subTest(name=name):
                self.assertIn(name, values)
                self.assertTrue(values[name])
        self.assertNotIn("NVIDIA_FALLBACK_MODEL_ID", values)
        self.assertEqual(values["QUIZ_HELPER_MODEL_ID"], "gpt-5.4")
        self.assertEqual(values["QUIZ_QUALITY_ARBITER_MODEL_ID"], "gpt-6.1-terra")

    def test_example_helper_parameters_are_valid_and_match_current_defaults(self):
        values = dotenv_values(Path(__file__).resolve().parents[2] / ".env.example")
        helper = {name: val for name, val in values.items() if name.startswith("QUIZ_HELPER_")}
        with patch.dict(os.environ, helper, clear=True):
            settings = HelperSettings.from_env()
            self.assertEqual(settings.model, "gpt-5.4")
            self.assertEqual(settings.reasoning_effort, "low")
            self.assertEqual(settings.max_output_tokens, 240)
            self.assertEqual(settings.timeout_seconds, 8)
            self.assertEqual(settings.max_attempts, 2)
            self.assertEqual([cautious_threshold(i) for i in (0, 2, 7)], [40, 50, 60])

    def test_custom_env_values_reach_the_production_responder(self):
        values = {
            "QUIZ_HELPER_MODEL_ID": "gpt-5.4-test",
            "QUIZ_HELPER_REASONING_EFFORT": "medium",
            "QUIZ_HELPER_TIMEOUT_SECONDS": "17.5",
            "QUIZ_HELPER_MAX_OUTPUT_TOKENS": "500",
            "QUIZ_HELPER_MAX_ATTEMPTS": "1",
            "QUIZ_HELPER_PLAUSIBILITY_LEVEL_0": "10",
            "QUIZ_HELPER_PLAUSIBILITY_LEVELS_1_3": "20",
            "QUIZ_HELPER_PLAUSIBILITY_LEVELS_4_15": "30",
        }
        payload = {
            "name": "Sam", "profession": "Historian", "gender": "male",
            "nationality": "", "language": "English", "outcome": "KNOW",
            "letter": "A", "question_text": "Which year?",
            "choices": {"A": "395", "B": "410", "C": "476", "D": "527"},
            "delivery_style": "CONFIDENT",
        }
        sdk = StubClient()
        with patch.dict(os.environ, values, clear=True):
            self.assertEqual(talk(payload, client=sdk), "I pick 395.")
            self.assertEqual(sdk.options[0], {"timeout": 17.5, "max_retries": 0})
            self.assertEqual(sdk.requests[0]["model"], "gpt-5.4-test")
            self.assertEqual(sdk.requests[0]["reasoning"], {"effort": "medium"})
            self.assertEqual(sdk.requests[0]["max_output_tokens"], 500)
            self.assertEqual(cautious_threshold(0), 10)
            self.assertEqual(cautious_threshold(2), 20)
            self.assertEqual(cautious_threshold(7), 30)
            self.assertEqual(best_visible_wrong(
                correct_key="A", difficulty=2,
                scores={"A": 100, "B": 25, "C": 10, "D": 8},
            ), "B")

    def test_invalid_settings_fail_loudly(self):
        for bad in (
            {"QUIZ_HELPER_MODEL_ID": ""},
            {"QUIZ_HELPER_REASONING_EFFORT": "whatever"},
            {"QUIZ_HELPER_MAX_OUTPUT_TOKENS": "NaN"},
            {"QUIZ_HELPER_TIMEOUT_SECONDS": "0"},
            {"QUIZ_HELPER_MAX_ATTEMPTS": "0"},
            {"QUIZ_HELPER_PLAUSIBILITY_LEVEL_0": "90",
             "QUIZ_HELPER_PLAUSIBILITY_LEVELS_1_3": "50"},
        ):
            with self.subTest(bad=bad), patch.dict(os.environ, bad, clear=True):
                with self.assertRaises(ValueError):
                    HelperSettings.from_env()


if __name__ == "__main__":
    unittest.main()
