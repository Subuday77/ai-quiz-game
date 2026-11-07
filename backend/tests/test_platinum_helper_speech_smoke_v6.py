"""V6 production-like smoke: compare the EXACT V5 cases and retain cost guard.

No real model call, no PostgreSQL access. Old plans remain unchanged.
"""
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from platinum_helper.settings import HelperSettings
from scripts import helper_live_regression as qa
from scripts import helper_speech_smoke_v5 as old
from scripts import helper_speech_smoke_v6 as new
from test_platinum_helper_speech_smoke_v4 import FakeResponses, pool


def baseline(path: Path):
    plan = old.make_plan(pool(), seed=20261107, model="gpt-5.4")
    plan["helper_settings"] = __import__("dataclasses").asdict(HelperSettings.from_env())
    path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    return plan


class V6SmokeTests(unittest.TestCase):
    def test_all_nine_cases_and_confidences_are_identical_to_v5(self):
        original = old.make_plan(pool(), seed=20261107, model="gpt-5.4")
        frozen = deepcopy(original)
        v6 = new.make_plan_from_v5(original, model="gpt-5.4")
        self.assertEqual(original, frozen)
        self.assertEqual(v6["scenarios"], original["scenarios"])
        self.assertEqual(v6["model"], original["model"])
        self.assertEqual(v6["seed"], original["seed"])
        self.assertEqual(v6["target"], 9)
        self.assertNotEqual(v6["version"], original["version"])

    def test_incompatible_or_modified_baselines_are_rejected(self):
        original = old.make_plan(pool(), seed=20261107, model="gpt-5.4")
        for modified in (
            {**original, "version": "other"},
            {**original, "model": "other"},
            {**original, "target": 5},
            {**original, "scenarios": original["scenarios"][:8]},
            {**original, "scenarios": [original["scenarios"][0]]*9},
        ):
            with self.subTest(m=modified["target"]):
                with self.assertRaises(ValueError):
                    new.make_plan_from_v5(modified, model="gpt-5.4")

    def test_free_preview_then_fake_paid_run_then_no_duplicate_calls(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "old_v5_plan.json"
            baseline(source)
            result = root / "new_v6"
            # Crucial: the V6 planner must not read current PostgreSQL questions
            # because new questions would destroy the controlled comparison.
            with patch.object(qa, "approved_questions", side_effect=AssertionError(
                     "V6 may not resample DB")), \
                 patch.object(new, "OpenAI", side_effect=lambda: FakeResponses()), \
                 patch.dict("os.environ", {"OPENAI_API_KEY": "fake-offline-key"}):
                args = ["--baseline-plan", str(source), "--output-dir", str(result)]
                self.assertEqual(new.main(args), 0)
                self.assertFalse((result / "calls.jsonl").exists())
                self.assertEqual(new.main(args + ["--execute"]), 0)
                summary = json.loads((result / "summary.json").read_text("utf-8"))
                self.assertEqual(summary["completed"], 9)
                self.assertEqual(summary["api_attempts"], 9)
                self.assertEqual(summary["fallbacks"], 0)
                self.assertEqual(summary["guard_rejections"], 0)
                self.assertEqual(new.main(args + ["--execute"]), 0)
                self.assertEqual(len(qa.read_jsonl(result / "attempts.jsonl")), 9)
                self.assertTrue((result / "platinum_helper_live_results.zip").is_file())
                # Modifying V5 baseline must NEVER silently resume existing plan.
                source.write_text(source.read_text("utf-8") + " ", encoding="utf-8")
                with self.assertRaisesRegex(SystemExit, "NEW --output-dir"):
                    new.main(args)

    def test_preview_fails_without_old_plan_before_any_spend(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(new, "OpenAI", side_effect=AssertionError(
                    "No API without plan")):
                with self.assertRaisesRegex(SystemExit, "V5 baseline plan not found"):
                    new.main(["--baseline-plan", str(Path(folder)/"missing.json"),
                              "--output-dir", str(Path(folder)/"new")])


if __name__ == "__main__":
    unittest.main()
