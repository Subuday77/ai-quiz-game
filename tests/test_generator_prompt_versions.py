"""Offline generator prompt migration and v1/v2 request invariants."""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from agents.generator import GENERATOR_PROMPT_V1, GENERATOR_PROMPT_V2
from consts import SHARED_DIFFICULTY_INSTRUCTIONS
from provider_result import Completion, ProviderResult
from question_generator import create_question, select_generator_prompt


class FakeGenerator:
    def __init__(self):
        self.calls = []
        self.responses = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return ProviderResult(
            '{"difficulty": 1, "question": "Какой цвет у травы?", '
            '"answers": {"A": "Зелёный", "B": "Красный", '
            '"C": "Чёрный", "D": "Синий"}, "correct_answer": "A"}',
            Completion.COMPLETE, "completed")


class GeneratorPromptTests(unittest.TestCase):
    def test_original_prompt_preserved_as_baseline(self):
        self.assertIn('similar to "Who Wants to Be a Millionaire?"', GENERATOR_PROMPT_V1)
        self.assertIn("Incorrect answers must be plausible.", GENERATOR_PROMPT_V1)
        self.assertIn("Never put the answer itself in the question", GENERATOR_PROMPT_V1)

    def test_new_prompt_explains_fair_distractor_construction(self):
        for marker in ("Levels 0–2", "Levels 3–5", "Levels 6–9",
                       "Levels 10–15", "near-identical greetings",
                       "TEST THE STRONGEST COMPETING WRONG ANSWER",
                       "LEGITIMATE REASONING VS ANSWER LEAKAGE",
                       "Randomize the location of the correct choice",
                       "EXACT requested integer difficulty"):
            with self.subTest(marker=marker):
                self.assertIn(marker, GENERATOR_PROMPT_V2)

    def test_single_shared_difficulty_scale_in_both_versions(self):
        for prompt in (GENERATOR_PROMPT_V1, GENERATOR_PROMPT_V2):
            self.assertEqual(prompt.count(SHARED_DIFFICULTY_INSTRUCTIONS), 1)

    def test_default_remains_original_until_ab_approved(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AI_GENERATOR_PROMPT_VERSION", None)
            self.assertIs(select_generator_prompt(), GENERATOR_PROMPT_V1)
        with patch.dict(os.environ, {"AI_GENERATOR_PROMPT_VERSION": "v2"}):
            self.assertIs(select_generator_prompt(), GENERATOR_PROMPT_V2)

    def test_invalid_version_fails_before_provider_request(self):
        with patch("question_generator.client") as provider:
            with self.assertRaises(ValueError):
                create_question(1, "Russian", "Music", prompt_version="v3")
            provider.assert_not_called()

    def test_generator_uses_same_model_settings_and_candidate_shape(self):
        fake = FakeGenerator()
        with patch("question_generator.client", return_value=fake):
            for version in ("v1", "v2"):
                question = create_question(1, "Russian", "Musical instruments",
                                           prompt_version=version)
                self.assertEqual(question["correct_answer"], "A")
                self.assertEqual(set(question["answers"]), set("ABCD"))
        self.assertEqual(len(fake.calls), 2)
        a, b = fake.calls
        self.assertIs(a["instructions"], GENERATOR_PROMPT_V1)
        self.assertIs(b["instructions"], GENERATOR_PROMPT_V2)
        for field in ("input", "max_output_tokens", "reasoning", "temperature"):
            self.assertEqual(a[field], b[field], field)
        self.assertEqual(a["reasoning"], {"effort": "none"})
        self.assertIn("difficulty level 1", a["input"])
        self.assertIn("Musical instruments", a["input"])

    def test_difficulty_bound_remains_checked_locally(self):
        with patch("question_generator.client") as provider:
            with self.assertRaises(ValueError):
                create_question(16, "Russian", "Music", prompt_version="v2")
            provider.assert_not_called()


if __name__ == "__main__":
    unittest.main()
