"""No-cost validation of the V4 nine-case plan and mixed-language reports."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID

from scripts import helper_speech_smoke_v4 as smoke
from scripts import helper_live_regression as qa


def pool():
    questions = []
    for code, language in enumerate(("German", "Russian", "English"), 1):
        for index in range(1, 6):
            ident = str(UUID(int=code * 100 + index))
            choices = {
                "A": f"Correct-{language}-{index}",
                "B": f"Near-{language}-{index}",
                "C": f"Almost-{language}-{index}",
                "D": f"Other-{language}-{index}",
            }
            questions.append({
                "question_id": ident, "language": language,
                "difficulty": 5, "category": "Inventions and discoveries",
                "topic": "Physics", "question": f"{language} question {index}?",
                "choices": choices, "correct_key": "A",
                "scores": {"A": 100, "B": 85, "C": 72, "D": 66},
            })
    return questions


class FakeResponses:
    """Deterministic test double, NOT a claim to simulate GPT-5.4 quality."""
    def __init__(self):
        self.calls = 0
        self.responses = self

    def with_options(self, **kwargs):
        return self

    def create(self, **kwargs):
        self.calls += 1
        instructions = kwargs["instructions"]
        question = kwargs["input"]
        if "SPEECH: your response." in instructions:
            if "ONLY in German" in instructions:
                speech = "Da muss ich leider passen."
            elif "ONLY in Russian" in instructions:
                speech = "Тут я, пожалуй, пас."
            else:
                speech = "I'm not certain enough to help."
            output = "SPEECH: " + speech
        else:
            import re
            letter, answer = re.search(
                r"Selected option: ([ABCD]): (.+)$", question, re.M
            ).groups()
            if "ONLY in German" in instructions:
                speech = f"Mein Tipp ist „{answer}“."
            elif "ONLY in Russian" in instructions:
                speech = f"Я бы поставил на «{answer}»."
            else:
                speech = f"I'd probably pick {answer}."
            output = f"KEY: {letter}\nSPEECH: {speech}"
        return SimpleNamespace(
            status="completed", output_text=output,
            usage=SimpleNamespace(input_tokens=100, output_tokens=35),
        )


class SpeechSmokeV4Tests(unittest.TestCase):
    def test_real_question_selection_produces_nine_balanced_cross_language_cases(self):
        plan = smoke.make_plan(pool(), seed=20261107, model="gpt-5.4")
        self.assertEqual(plan["target"], 9)
        scenarios = plan["scenarios"]
        self.assertEqual(len(set(x["question_id"] for x in scenarios)), 9)
        self.assertEqual(
            [(x["language"], x["speech_language"], x["effective_outcome"])
             for x in scenarios],
            list(smoke.CASES),
        )
        self.assertEqual(
            {outcome: sum(x["effective_outcome"] == outcome for x in scenarios)
             for outcome in ("KNOW", "MISTAKEN", "UNSURE")},
            {"KNOW": 3, "MISTAKEN": 3, "UNSURE": 3},
        )
        self.assertEqual(
            {x["delivery_style"] for x in scenarios if x["effective_outcome"] != "UNSURE"},
            {"TENTATIVE", "CONFIDENT"},
        )
        for x in scenarios:
            data = qa.responder_data(x)
            self.assertEqual(data["language"], x["language"])
            self.assertEqual(data["speech_language"], x["speech_language"])
            if x["effective_outcome"] == "MISTAKEN":
                self.assertNotEqual(x["selected_key"], x["correct_key"])
                self.assertNotIn(x["selected_key"], x["hidden_keys"])

    def test_missing_german_pool_fails_without_invented_questions(self):
        with self.assertRaisesRegex(RuntimeError, "German approved questions"):
            smoke.make_plan(
                [item for item in pool() if item["language"] != "German"],
                seed=20261107, model="gpt-5.4",
            )

    def test_preview_is_free_resumable_and_rejects_modified_responder_code(self):
        with tempfile.TemporaryDirectory() as temp, \
             patch.object(qa, "approved_questions", return_value=pool()) as fetch, \
             patch.object(smoke, "OpenAI", side_effect=AssertionError("paid call")):
            self.assertEqual(smoke.main(["--output-dir", temp]), 0)
            self.assertEqual(smoke.main(["--output-dir", temp]), 0)
            self.assertEqual(fetch.call_count, 1)
            self.assertTrue((Path(temp) / "plan.json").exists())
            self.assertFalse((Path(temp) / "attempts.jsonl").exists())
            with patch.object(qa, "code_fingerprint", return_value="different"):
                with self.assertRaisesRegex(SystemExit, "NEW --output-dir"):
                    smoke.main(["--output-dir", temp])

    def test_offline_fake_client_exercises_all_nine_without_cost(self):
        with tempfile.TemporaryDirectory() as temp, \
             patch.object(qa, "approved_questions", return_value=pool()), \
             patch.object(smoke, "OpenAI", side_effect=lambda: FakeResponses()), \
             patch.dict("os.environ", {"OPENAI_API_KEY": "offline-fake-value"}):
            self.assertEqual(smoke.main(["--output-dir", temp]), 0)
            self.assertEqual(smoke.main(["--output-dir", temp, "--execute"]), 0)
            summary = json.loads((Path(temp) / "summary.json").read_text("utf-8"))
            self.assertEqual(summary["completed"], 9)
            self.assertEqual(summary["api_attempts"], 9)
            self.assertEqual(summary["fallbacks"], 0)
            self.assertEqual(summary["guard_rejections"], 0)
            self.assertEqual(summary["literal_correct_leaks"], 0)
            self.assertEqual(len(summary["question_ui_pairs"]), 9)
            self.assertTrue((Path(temp) / "platinum_helper_live_results.zip").exists())
            # Resuming does not bill/double-call or mutate completed cases.
            self.assertEqual(smoke.main(["--output-dir", temp, "--execute"]), 0)
            self.assertEqual(len(qa.read_jsonl(Path(temp) / "attempts.jsonl")), 9)


if __name__ == "__main__":
    unittest.main()
