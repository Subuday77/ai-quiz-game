"""Prompt regression checks for critic v2 answer-leak editorial guidance."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from agents.critic_v2 import QUALITY_REVIEW_V2_PROMPT


class CriticLeakPromptTests(unittest.TestCase):
    def test_real_game_examples_are_covered(self):
        for phrase in ("лоб/лобная", "Соедини четыре", "shared word stems",
                       "leakage=fail", "answer_leak"):
            self.assertIn(phrase, QUALITY_REVIEW_V2_PROMPT)

    def test_negative_examples_avoid_overfiltering(self):
        for phrase in ("Закон Ома", "Хуан Рульфо",
                       "Do NOT treat ordinary subject vocabulary"):
            self.assertIn(phrase, QUALITY_REVIEW_V2_PROMPT)


if __name__ == "__main__":
    unittest.main()
