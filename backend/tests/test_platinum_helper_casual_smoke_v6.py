"""Offline safety/resume tests for the 3-case CASUAL follow-up.

No live DB and no OpenAI network calls.
"""
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from platinum_helper.settings import HelperSettings
from scripts import helper_casual_smoke_v6 as target
from scripts import helper_live_regression as qa
from scripts import helper_speech_regression_v6_30 as previous
from test_platinum_helper_speech_regression_v6_30 import FakeModel, approved_pool


def saved_baseline(path: Path) -> dict:
    from dataclasses import asdict
    plan = previous.build_plan(approved_pool(), 20261107, "gpt-5.4")
    plan["helper_settings"] = asdict(HelperSettings.from_env())
    plan["code_fingerprint"] = "original-version-pinned"
    qa.save_json(path, plan)
    return plan


class CasualFollowUpTests(unittest.TestCase):
    def test_three_exact_prior_cases_are_reused(self):
        with tempfile.TemporaryDirectory() as temp:
            original = saved_baseline(Path(temp) / "original.json")
            frozen = deepcopy(original)
            cases = target.sample_previous(original, model="gpt-5.4")
            self.assertEqual(original, frozen)
            self.assertEqual([s["id"] for s in cases],
                             ["S007", "S016", "S023"])
            self.assertEqual(
                [s["speech_language"] for s in cases],
                ["ru", "ja", "ar"],
            )
            self.assertEqual(
                [s["delivery_style"] for s in cases],
                ["TENTATIVE", "CONFIDENT", "TENTATIVE"],
            )
            for s in cases:
                self.assertEqual(s, next(
                    x for x in original["scenarios"] if x["id"] == s["id"]
                ))

    def test_edited_or_wrong_source_fails_before_paid_calls(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "old.json"
            original = saved_baseline(path)
            altered = deepcopy(original)
            altered["scenarios"][6]["presentation_style"] = "DIRECT"
            with self.assertRaisesRegex(ValueError, "Unexpected baseline"):
                target.sample_previous(altered, model="gpt-5.4")
            with self.assertRaisesRegex(ValueError, "authentic V6"):
                target.sample_previous({**original, "version": "other"},
                                       model="gpt-5.4")

    def test_free_preview_and_fake_execute_then_idempotent_resume(self):
        model = FakeModel()
        with tempfile.TemporaryDirectory() as temp, \
             patch.object(target, "OpenAI", return_value=model), \
             patch.dict("os.environ", {"OPENAI_API_KEY": "offline-fake"}), \
             patch.object(qa, "approved_questions",
                          side_effect=AssertionError("DB must not be touched")):
            source = Path(temp) / "v6_30.json"
            saved_baseline(source)
            folder = Path(temp) / "v6_casual_fix"
            args = ["--baseline-plan", str(source),
                    "--output-dir", str(folder)]
            self.assertEqual(target.main(args), 0)
            self.assertFalse((folder / "attempts.jsonl").exists())
            self.assertEqual(target.main(args + ["--execute"]), 0)
            summary = json.loads((folder / "summary.json").read_text("utf-8"))
            self.assertEqual(summary["completed"], 3)
            self.assertEqual(summary["api_attempts"], 3)
            self.assertEqual(summary["guard_rejections"], 0)
            self.assertEqual(summary["fallbacks"], 0)
            self.assertEqual(len(model.requests), 3)
            self.assertTrue((folder / "platinum_helper_live_results.zip").is_file())
            self.assertEqual(target.main(args + ["--execute"]), 0)
            self.assertEqual(len(model.requests), 3)
            with patch.object(qa, "code_fingerprint", return_value="updated"):
                with self.assertRaisesRegex(SystemExit, "NEW --output-dir"):
                    target.main(args)

    def test_missing_baseline_fails_before_api(self):
        with tempfile.TemporaryDirectory() as temp, \
             patch.object(target, "OpenAI",
                          side_effect=AssertionError("No paid API")):
            with self.assertRaisesRegex(SystemExit, "Missing original V6/30"):
                target.main([
                    "--baseline-plan", str(Path(temp) / "missing.json"),
                    "--output-dir", str(Path(temp) / "output"),
                ])


if __name__ == "__main__":
    unittest.main()
