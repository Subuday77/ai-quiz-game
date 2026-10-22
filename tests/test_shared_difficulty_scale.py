"""Generator and v2 critic must consume one authoritative difficulty scale."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from consts import (DIFFICULTY_LEVELS, SHARED_DIFFICULTY_INSTRUCTIONS,
                    QUESTION_PROMPT)
from agents.critic_v2 import QUALITY_REVIEW_V2_PROMPT


class SharedDifficultyTests(unittest.TestCase):
    def test_all_sixteen_levels(self):
        self.assertEqual(len(DIFFICULTY_LEVELS), 16)
        self.assertEqual(len(set(DIFFICULTY_LEVELS)), 16)
        for level, description in enumerate(DIFFICULTY_LEVELS):
            self.assertTrue(description.startswith(f"{level} —"))

    def test_generator_and_reviewer_use_exact_same_instructions(self):
        self.assertIn(SHARED_DIFFICULTY_INSTRUCTIONS, QUESTION_PROMPT)
        self.assertIn(SHARED_DIFFICULTY_INSTRUCTIONS, QUALITY_REVIEW_V2_PROMPT)
        self.assertEqual(QUESTION_PROMPT.count(SHARED_DIFFICULTY_INSTRUCTIONS), 1)
        self.assertEqual(QUALITY_REVIEW_V2_PROMPT.count(SHARED_DIFFICULTY_INSTRUCTIONS), 1)

    def test_low_levels_are_not_penalized_for_being_easy(self):
        self.assertIn("level 0 or 1 is SUPPOSED to be easy", SHARED_DIFFICULTY_INSTRUCTIONS)
        self.assertIn("ordinary educated adult", SHARED_DIFFICULTY_INSTRUCTIONS)


if __name__ == "__main__":
    unittest.main()
