"""V5 small QA: no-cost dry run, all three independent presentation frames.

Runs the production responder against a fake model. Real GPT-5.4 speech
quality is NOT evaluated here; this verifies only test infrastructure.
"""
import json
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

from scripts import helper_speech_smoke_v5 as smoke
from scripts import helper_live_regression as qa
from test_platinum_helper_speech_smoke_v4 import FakeResponses, pool


class V5SpeechSmokeTests(unittest.TestCase):
    def test_nine_slots_cover_every_frame_outcome_and_question_language(self):
        p = smoke.make_plan(pool(), seed=20261107, model="gpt-5.4")
        self.assertEqual(p["version"], smoke.PLAN_VERSION)
        self.assertEqual(len(p["scenarios"]), 9)
        for lang in ("German", "Russian", "English"):
            group = [s for s in p["scenarios"] if s["language"] == lang]
            self.assertEqual({s["presentation_style"] for s in group},
                             {"DIRECT", "PERSONAL", "CASUAL"})
            self.assertEqual({s["effective_outcome"] for s in group},
                             {"KNOW", "MISTAKEN", "UNSURE"})
        for outcome in ("KNOW", "MISTAKEN", "UNSURE"):
            group = [s for s in p["scenarios"] if s["effective_outcome"] == outcome]
            self.assertEqual({s["presentation_style"] for s in group},
                             {"DIRECT", "PERSONAL", "CASUAL"})
        for style in ("DIRECT", "PERSONAL", "CASUAL"):
            group = [s for s in p["scenarios"] if
                     s["presentation_style"] == style and s["effective_outcome"] != "UNSURE"]
            self.assertEqual({s["delivery_style"] for s in group},
                             {"CONFIDENT", "TENTATIVE"})
        self.assertEqual(len({s["question_id"] for s in p["scenarios"]}), 9)
        self.assertEqual(Counter(s["presentation_style"] for s in p["scenarios"]),
                         {"DIRECT": 3, "PERSONAL": 3, "CASUAL": 3})
        for s in p["scenarios"]:
            data = qa.responder_data(s)
            self.assertEqual(data["presentation_style"], s["presentation_style"])
            self.assertEqual(data["speech_language"], s["speech_language"])

    def test_preview_and_fake_execute_nine_without_duplicate_calls(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(qa, "approved_questions", return_value=pool()), \
             patch.object(smoke, "OpenAI", side_effect=lambda: FakeResponses()), \
             patch.dict("os.environ", {"OPENAI_API_KEY": "fake-test-key"}):
            self.assertEqual(smoke.main(["--output-dir", folder]), 0)
            self.assertFalse((Path(folder) / "attempts.jsonl").exists())
            self.assertEqual(smoke.main(["--execute", "--output-dir", folder]), 0)
            summary = json.loads((Path(folder) / "summary.json").read_text("utf-8"))
            self.assertEqual(summary["completed"], 9)
            self.assertEqual(summary["api_attempts"], 9)
            self.assertEqual(summary["guard_rejections"], 0)
            self.assertEqual(summary["fallbacks"], 0)
            self.assertEqual(summary["literal_correct_leaks"], 0)
            records = qa.read_jsonl(Path(folder) / "calls.jsonl")
            self.assertEqual(len(records), 9)
            self.assertEqual(smoke.main(["--execute", "--output-dir", folder]), 0)
            self.assertEqual(len(qa.read_jsonl(Path(folder) / "attempts.jsonl")), 9)
            self.assertTrue((Path(folder) / "platinum_helper_live_results.zip").exists())

    def test_missing_pool_fails_before_paid_api(self):
        with self.assertRaisesRegex(RuntimeError, "German approved questions"):
            smoke.make_plan([q for q in pool() if q["language"] != "German"],
                            seed=20261107, model="gpt-5.4")

    def test_pinned_plan_rejects_changed_core_frame_logic(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(qa, "approved_questions", return_value=pool()):
            self.assertEqual(smoke.main(["--output-dir", folder]), 0)
            with patch.object(qa, "code_fingerprint", return_value="different"):
                with self.assertRaisesRegex(SystemExit, "NEW --output-dir"):
                    smoke.main(["--output-dir", folder])


if __name__ == "__main__":
    unittest.main()
