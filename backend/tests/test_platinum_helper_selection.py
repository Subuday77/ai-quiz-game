"""Offline regression for generator-score-aware helper mistakes."""
import unittest
from random import Random

from platinum_helper.selection import best_visible_wrong, cautious_threshold


class PlausibleMistakeTests(unittest.TestCase):
    def test_cautious_threshold_by_difficulty(self):
        self.assertEqual([cautious_threshold(n) for n in (0, 1, 3, 4, 8, 14)],
                         [40, 50, 50, 60, 60, 60])
        with self.assertRaises(ValueError):
            cautious_threshold(16)

    def test_prefer_best_wrong_from_generator(self):
        scores = {"A": 100, "B": 8, "C": 12, "D": 10}
        # Boat (C) wins, but its score 12 cannot pass even difficulty 0.
        self.assertIsNone(best_visible_wrong(correct_key="A", difficulty=0, scores=scores))
        self.assertEqual(best_visible_wrong(
            correct_key="A", difficulty=0,
            scores={"A": 100, "B": 44, "C": 12, "D": 10}), "B")

    def test_hidden_bronze_answers_are_never_selected(self):
        self.assertEqual(best_visible_wrong(
            correct_key="C", difficulty=3,
            scores={"A": 88, "B": 90, "C": 100, "D": 10},
            hidden=("B", "D")), "A")
        self.assertIsNone(best_visible_wrong(
            correct_key="C", difficulty=3,
            scores={"A": 88, "B": 90, "C": 100, "D": 10},
            hidden=("B", "D", "A")))

    def test_difficulty_can_force_abstention(self):
        scores = {"A": 55, "B": 10, "C": 100, "D": 0}
        self.assertEqual(best_visible_wrong(
            correct_key="C", difficulty=2, scores=scores), "A")
        self.assertIsNone(best_visible_wrong(
            correct_key="C", difficulty=5, scores=scores))

    def test_legacy_and_invalid_metadata_abstain(self):
        self.assertIsNone(best_visible_wrong(
            correct_key="C", difficulty=1, scores=None))
        self.assertIsNone(best_visible_wrong(
            correct_key="C", difficulty=7, scores=None))
        self.assertIsNone(best_visible_wrong(
            correct_key="C", difficulty=0, scores={"A": 50, "B": 50}))
        self.assertIsNone(best_visible_wrong(
            correct_key="C", difficulty=1, scores={"A": True, "B": 0, "D": 0}))
        self.assertEqual(best_visible_wrong(
            correct_key="C", difficulty=4,
            scores={"A": 60, "B": 60, "C": 100, "D": 20},
            hidden=("A",), rng=Random(4)), "B")


if __name__ == "__main__":
    unittest.main()
