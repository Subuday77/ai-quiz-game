"""Offline QA harness tests: 30 real-shaped questions, all 11 UI languages.

The fake model never makes an OpenAI request or requires a live database.
It verifies the same speech validators, 30-case freezing, budget and resume.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from collections import Counter
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID

from scripts import helper_speech_regression_v6_30 as smoke
from scripts import helper_live_regression as qa
from test_platinum_helper_speech_smoke_v4 import pool as small_pool


LANGUAGE_NAMES = {
    "en": "English", "de": "German", "ru": "Russian",
    "fr": "French", "es": "Spanish", "it": "Italian",
    "pt": "Portuguese", "he": "Hebrew", "ar": "Arabic",
    "ja": "Japanese", "zh": "Chinese",
}
UNSURE = {
    "en": "I'm not sure about this one.",
    "de": "Hier weiß ich leider nicht weiter.",
    "ru": "Не знаю, что подсказать.",
    "fr": "Je ne sais vraiment pas.",
    "es": "No estoy seguro de esto.",
    "it": "Non ne sono sicuro.",
    "pt": "Não sei o que indicar.",
    "he": "אני לא בטוח בתשובה.",
    "ar": "لست متأكدا من الإجابة.",
    "ja": "ごめん、わからない。",
    "zh": "这个我不确定。",
}
ADVICE = {
    "en": "Pick {answer}.",
    "de": "Mein Tipp ist „{answer}“.",
    "ru": "Я за «{answer}».",
    "fr": "Je vote pour «{answer}».",
    "es": "Elige «{answer}».",
    "it": "Vai con «{answer}».",
    "pt": "Escolhe «{answer}».",
    "he": "אני בוחר {answer}.",
    "ar": "اختر {answer}.",
    "ja": "{answer}を選んで。",
    "zh": "我选{answer}。",
}


def approved_pool():
    source = small_pool()
    return [
        {**row, "question_id": str(UUID(int=5000 + copy*len(source) + i)),
         "question": f"{row['language']} verified item {copy}-{i}?"}
        for copy in range(4) for i, row in enumerate(source)
    ]


class FakeModel:
    def __init__(self):
        self.requests = []
        self.responses = self

    def with_options(self, **kwargs):
        return self

    def create(self, **kwargs):
        import re
        self.requests.append(kwargs)
        instructions, payload = kwargs["instructions"], kwargs["input"]
        code = next(code for code, name in LANGUAGE_NAMES.items()
                    if "ONLY in " + name in instructions)
        if "Selected option:" not in payload:
            output = "SPEECH: " + UNSURE[code]
        else:
            letter, answer = re.search(
                r"Selected option: ([ABCD]): (.+)$", payload, re.M
            ).groups()
            output = f"KEY: {letter}\nSPEECH: " + ADVICE[code].format(answer=answer)
        return SimpleNamespace(
            status="completed", output_text=output,
            usage=SimpleNamespace(input_tokens=100, output_tokens=35),
        )


class V6ThirtyScenarioTests(unittest.TestCase):
    def test_plan_is_balanced_unique_multilingual_and_reproducible(self):
        questions = approved_pool()
        plan = smoke.build_plan(questions, 20261107, "gpt-5.4")
        again = smoke.build_plan(questions, 20261107, "gpt-5.4")
        self.assertEqual(plan["scenarios"], again["scenarios"])
        rows = plan["scenarios"]
        self.assertEqual(len(rows), 30)
        self.assertEqual(len({x["question_id"] for x in rows}), 30)
        self.assertEqual(Counter(x["effective_outcome"] for x in rows),
                         {"KNOW": 10, "MISTAKEN": 10, "UNSURE": 10})
        self.assertEqual(Counter(x["presentation_style"] for x in rows),
                         {"DIRECT": 10, "PERSONAL": 10, "CASUAL": 10})
        self.assertEqual(Counter(x["delivery_style"] for x in rows
                                 if x["effective_outcome"] != "UNSURE"),
                         {"CONFIDENT": 10, "TENTATIVE": 10})
        self.assertEqual(set(x["speech_language"] for x in rows),
                         set(LANGUAGE_NAMES))
        for source_code in ("de", "ru", "en"):
            self.assertGreaterEqual(sum(
                smoke.code(x["language"]) == source_code for x in rows
            ), 3)
        for index, item in enumerate(rows):
            self.assertEqual(item["speech_language"], smoke.SPECS[index][1])
            self.assertEqual(item["effective_outcome"], smoke.SPECS[index][2])
            if item["effective_outcome"] == "MISTAKEN":
                self.assertNotEqual(item["correct_key"], item["selected_key"])
                self.assertNotIn(item["selected_key"], item["hidden_keys"])
            self.assertEqual(
                qa.responder_data(item)["presentation_style"],
                item["presentation_style"],
            )

    def test_few_german_questions_falls_back_to_other_source_languages(self):
        pool = approved_pool()
        german = [x for x in pool if x["language"] == "German"][:3]
        other = [x for x in pool if x["language"] != "German"]
        plan = smoke.build_plan(german + other, 20261107, "gpt-5.4")
        self.assertEqual(sum(x["language"] == "German" for x in plan["scenarios"]), 3)
        self.assertEqual(plan["target"], 30)

    def test_missing_german_questions_fails_before_paying(self):
        with self.assertRaisesRegex(RuntimeError, "not enough approved"):
            smoke.build_plan(
                [x for x in approved_pool() if x["language"] != "German"],
                20261107, "gpt-5.4",
            )

    def test_preview_is_free_and_resume_keeps_exact_plan(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(qa, "approved_questions", return_value=approved_pool()) as db, \
             patch.object(smoke, "OpenAI", side_effect=AssertionError("paid API")):
            args = ["--output-dir", tmp]
            self.assertEqual(smoke.main(args), 0)
            self.assertEqual(smoke.main(args), 0)
            self.assertEqual(db.call_count, 1)
            path = Path(tmp) / "plan.json"
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(len(saved["scenarios"]), 30)
            self.assertFalse((Path(tmp) / "attempts.jsonl").exists())
            with patch.object(qa, "code_fingerprint", return_value="changed"):
                with self.assertRaisesRegex(SystemExit, "NEW --output-dir"):
                    smoke.main(args)
            changed = deepcopy(saved)
            changed["scenarios"][0]["speech_language"] = "xx"
            path.write_text(json.dumps(changed), encoding="utf-8")
            with self.assertRaisesRegex(SystemExit, "Saved S001 invalid"):
                smoke.main(args)

    def test_fake_full_run_traces_30_with_all_real_script_checks(self):
        fake = FakeModel()
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(qa, "approved_questions", return_value=approved_pool()), \
             patch.object(smoke, "OpenAI", return_value=fake), \
             patch.dict("os.environ", {"OPENAI_API_KEY": "offline-test-only"}):
            args = ["--output-dir", tmp]
            self.assertEqual(smoke.main(args), 0)
            self.assertEqual(smoke.main(args + ["--execute"]), 0)
            summary = json.loads((Path(tmp) / "summary.json").read_text("utf-8"))
            self.assertEqual(summary["completed"], 30)
            self.assertEqual(summary["api_attempts"], 30)
            self.assertEqual(summary["fallbacks"], 0)
            self.assertEqual(summary["guard_rejections"], 0)
            self.assertEqual(summary["literal_correct_leaks"], 0)
            self.assertEqual(summary["em_dash_speeches"], 0)
            self.assertEqual(len(fake.requests), 30)
            self.assertTrue((Path(tmp) / "platinum_helper_live_results.zip").exists())
            self.assertEqual(smoke.main(args + ["--execute"]), 0)
            self.assertEqual(len(fake.requests), 30)

    def test_orphan_attempt_blocks_automatic_paid_replay(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(qa, "approved_questions", return_value=approved_pool()), \
             patch.object(smoke, "OpenAI", return_value=FakeModel()), \
             patch.dict("os.environ", {"OPENAI_API_KEY": "offline-test-only"}):
            args = ["--output-dir", tmp]
            smoke.main(args)
            qa.append_jsonl(Path(tmp) / "attempts.jsonl", {
                "id": "S001", "input_tokens": 100, "output_tokens": 20,
            })
            with self.assertRaisesRegex(RuntimeError, "Uncommitted API attempts"):
                smoke.main(args + ["--execute"])
            self.assertEqual(qa.read_jsonl(Path(tmp) / "calls.jsonl"), [])

    def test_attempt_cap_stops_before_overrun(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(qa, "approved_questions", return_value=approved_pool()), \
             patch.object(smoke, "OpenAI", return_value=FakeModel()), \
             patch.dict("os.environ", {"OPENAI_API_KEY": "offline-test-only"}):
            args = ["--output-dir", tmp]
            smoke.main(args)
            smoke.main(args + ["--execute", "--max-api-requests", "2"])
            result = json.loads((Path(tmp) / "summary.json").read_text("utf-8"))
            self.assertEqual(result["completed"], 1)
            self.assertEqual(result["api_attempts"], 1)


if __name__ == "__main__":
    unittest.main()
