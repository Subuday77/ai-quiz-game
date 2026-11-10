"""Offline tests for the read-only live QA runner; never call OpenAI or PostgreSQL."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import NAMESPACE_URL, uuid5
from zipfile import ZipFile

from helper_fallback_fixtures import english_templates, russian_templates
from scripts import helper_live_regression as qa


def sample_question(index, plausible):
    return {
        "question_id": str(uuid5(NAMESPACE_URL, f"helper-qa-test-{index}")),
        "language": "Russian" if index % 3 else "English",
        "difficulty": 3,
        "category": "Modern history",
        "topic": "History",
        "question": f"Sample historical event {index}?",
        "choices": {"A": "476", "B": "395", "C": "410", "D": "527"},
        "correct_key": "A",
        "scores": {"A": 100, "B": 85 if plausible else 12,
                   "C": 20 if plausible else 10, "D": 10 if plausible else 8},
    }


class FakeSDK:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.api_calls = 0

    def with_options(self, **kwargs):
        assert kwargs["max_retries"] == 0 and kwargs["timeout"] == 8.0
        return self

    @property
    def responses(self):
        return self

    def create(self, **kwargs):
        self.api_calls += 1
        response = next(self.replies)
        if isinstance(response, Exception):
            raise response
        return SimpleNamespace(
            status="completed", output_text=response,
            usage=SimpleNamespace(input_tokens=100, output_tokens=20),
        )


class HelperLiveQATests(unittest.TestCase):
    def setUp(self):
        self.questions = [sample_question(i, i < 30) for i in range(50)]

    def test_reproducible_plan_and_forced_outcomes(self):
        first = qa.build_plan(self.questions, 20, 123, "gpt-5.4")
        other = qa.build_plan(self.questions, 20, 123, "gpt-5.4")
        self.assertEqual(first["scenarios"], other["scenarios"])
        self.assertEqual(len({s["question_id"] for s in first["scenarios"]}), 20)
        self.assertEqual(
            dict(__import__("collections").Counter(
                s["effective_outcome"] for s in first["scenarios"]
            )),
            {"KNOW": 6, "MISTAKEN": 8, "UNSURE": 6},
        )
        for item in first["scenarios"]:
            if item["effective_outcome"] == "MISTAKEN":
                self.assertNotEqual(item["selected_key"], item["correct_key"])
                self.assertNotIn(item["selected_key"], item["hidden_keys"])
            if item["requested_outcome"] == "MISTAKEN" and item["effective_outcome"] == "UNSURE":
                self.assertIsNone(item["selected_key"])

    def test_real_bronze_and_threshold_match_pure_helpers(self):
        q = sample_question(200, True)
        result = qa.scenario(q, "MISTAKEN", 1, 123)
        self.assertEqual(result["selected_key"], "B")
        self.assertEqual(len(result["hidden_keys"]), 1)
        self.assertNotIn("B", result["hidden_keys"])
        self.assertIsNone(qa.scenario(sample_question(300, False), "MISTAKEN", 1, 123))
        abstain = qa.scenario(sample_question(300, False), "ABSTAIN", 1, 123)
        self.assertEqual(abstain["effective_outcome"], "UNSURE")

    def test_guardrail_single_attempt_and_fallback_are_traced(self):
        s = qa.scenario(sample_question(500, True), "MISTAKEN", 0, 123)
        with tempfile.TemporaryDirectory() as temp:
            attempts = Path(temp) / "attempts.jsonl"
            sdk = FakeSDK([
                "KEY: B\nSPEECH: Но правильный ответ 476.",
                "KEY: B\nSPEECH: Я бы выбрал 395.",
            ])
            result = qa.evaluate(s, sdk, attempts, 2.5, 15.0, fallback_loader=russian_templates)
            self.assertEqual(result["status"], "completed", result)
            self.assertEqual(result["guard_rejections"], 1)
            self.assertEqual(result["api_requests"], 1)
            self.assertTrue(result["is_fallback"])
            self.assertIn("395", result["speech"])
            self.assertEqual(len(qa.read_jsonl(attempts)), 1)
            self.assertGreater(result["estimated_usd"], 0)

    def test_invalid_draft_immediately_uses_production_fallback(self):
        s = qa.scenario(sample_question(600, True), "MISTAKEN", 0, 123)
        with tempfile.TemporaryDirectory() as temp:
            result = qa.evaluate(s, FakeSDK([
                "KEY: B\nSPEECH: Правильный ответ 476.",
                "KEY: B\nSPEECH: Ответ 476.",
            ]), Path(temp) / "attempts.jsonl", 2.5, 15.0,
            fallback_loader=english_templates)
            self.assertEqual(result["status"], "completed", result)
            self.assertTrue(result["is_fallback"])
            self.assertEqual(result["guard_rejections"], 1)
            self.assertEqual(result["api_requests"], 1)
            self.assertFalse(result["literal_correct_leak"])

    def test_script_loads_the_existing_root_env(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(qa, "approved_questions", return_value=self.questions), patch.object(
                qa, "load_dotenv"
            ) as mocked_env:
                self.assertEqual(qa.main([
                    "--target", "10", "--limit", "3", "--output-dir", temp,
                ]), 0)
                mocked_env.assert_called_once_with(
                    qa.BACKEND.parent / ".env", override=False,
                )

    def test_preview_never_creates_provider_or_calls_api(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(qa, "approved_questions", return_value=self.questions), patch.object(
                qa, "OpenAI", side_effect=AssertionError("API called during preview")
            ):
                self.assertEqual(qa.main([
                    "--target", "10", "--limit", "3", "--output-dir", temp,
                ]), 0)
                plan = json.loads((Path(temp) / "plan.json").read_text(encoding="utf-8"))
                self.assertEqual(len(plan["scenarios"]), 10)
                self.assertFalse((Path(temp) / "calls.jsonl").exists())

    def test_updated_responder_code_requires_a_fresh_run_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(qa, "approved_questions", return_value=self.questions):
                self.assertEqual(qa.main([
                    "--target", "10", "--limit", "3", "--output-dir", temp,
                ]), 0)
            with patch.object(qa, "code_fingerprint", return_value="changed"):
                with self.assertRaisesRegex(SystemExit, "Use a NEW --output-dir"):
                    qa.main(["--target", "10", "--limit", "3", "--output-dir", temp])

    def test_report_export_and_resume_idempotence(self):
        plan = qa.build_plan(self.questions, 10, 123, "gpt-5.4")
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            qa.append_jsonl(directory / "calls.jsonl", {
                "id": "S001", "status": "completed", "speech": "Не знаю.",
                "api_requests": 1, "input_tokens": 100, "output_tokens": 20,
                "guard_rejections": 0, "is_fallback": False,
            })
            qa.append_jsonl(directory / "attempts.jsonl", {
                "id": "S001", "input_tokens": 100, "output_tokens": 20,
            })
            archive = qa.export(directory, plan, 2.5, 15.0)
            self.assertEqual(qa.latest(directory)["S001"]["status"], "completed")
            self.assertTrue(archive.is_file())
            with ZipFile(archive) as z:
                self.assertIn("blind_review.csv", z.namelist())
                self.assertIn("results.csv", z.namelist())
                self.assertEqual(json.loads(z.read("summary.json"))["completed"], 1)


if __name__ == "__main__":
    unittest.main()
