"""Regression tests for the extracted critic skill (no API calls)."""
import sys
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from agents.critic import QUALITY_REVIEW_PROMPT, SKILL_PATH, SKILL_VERSION, load_critic_skill


class CriticSkillTests(unittest.TestCase):
    def test_skill_is_versioned_and_loaded_verbatim(self):
        self.assertEqual(SKILL_VERSION, "1.0.0")
        self.assertTrue(SKILL_PATH.is_file())
        self.assertEqual(QUALITY_REVIEW_PROMPT, SKILL_PATH.read_text(encoding="utf-8"))
        self.assertIs(QUALITY_REVIEW_PROMPT, load_critic_skill())

    def test_existing_contract_instructions_preserved(self):
        prompt = QUALITY_REVIEW_PROMPT
        for phrase in (
            "Review one quiz candidate.",
            "English, Russian, Hebrew, German, French,",
            "decision: accept | regenerate | escalate",
            "independent_answer: A | B | C | D | unknown",
            "No external references were consulted",
        ):
            self.assertIn(phrase, prompt)


if __name__ == "__main__":
    unittest.main()
