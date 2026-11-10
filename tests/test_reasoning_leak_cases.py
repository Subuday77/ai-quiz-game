"""Regression checks for the v2 critic's reasoning-vs-leakage benchmark."""
import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from agents.critic_v2 import QUALITY_REVIEW_V2_PROMPT
from question_schema import QuizQuestion


def fixture_cases():
    script = ast.parse((ROOT / "test_openai_reviewer_v2.py").read_text(encoding="utf-8"))
    for stmt in script.body:
        if isinstance(stmt, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "REASONING_CASES"
            for target in stmt.targets
        ):
            return ast.literal_eval(stmt.value)
    raise AssertionError("REASONING_CASES not found")


class ReasoningLeakTests(unittest.TestCase):
    def test_prompt_preserves_legitimate_reasoning(self):
        prompt = QUALITY_REVIEW_V2_PROMPT
        for phrase in ("REASONING VERSUS LEAKAGE", "DECISIVE LINK",
                       "REMEMBER an additional fact", "Difficulty must be judged separately"):
            self.assertIn(phrase, prompt)

    def test_eight_playtest_cases_plus_one_rewrite(self):
        cases = fixture_cases()
        self.assertEqual(len(cases), 9)
        self.assertEqual([case[-1] for case in cases[:8]],
                         ["pass", "fail", "pass", "fail", "pass", "fail", "pass", "fail"])
        self.assertEqual(cases[8][-1], "pass")
        self.assertEqual(cases[5][3], cases[8][3])
        self.assertEqual(cases[5][4], cases[8][4])
        self.assertNotIn("пергамент", cases[8][2].casefold())
        self.assertIn("пергамент", cases[5][2].casefold())
        for difficulty, category, stem, answers, key, expected in cases:
            self.assertIn(expected, ("pass", "fail"))
            question = QuizQuestion.model_validate({
                "difficulty": difficulty, "question": stem,
                "answers": dict(zip("ABCD", answers)),
                "correct_answer": key,
            })
            self.assertEqual(len(question.answers), 4)


if __name__ == "__main__":
    unittest.main()
