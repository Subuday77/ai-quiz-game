"""Choice permutation regression: immutable Question keys and per-attempt display keys."""
import unittest

from persistence.choice_order import (
    canonical_key, display_choices, display_key, display_scores, new_choice_order,
)


class ChoiceOrderTests(unittest.TestCase):
    def test_legacy_order_is_identity(self):
        values = {"A": "wrong", "B": "correct", "C": "wrong2", "D": "wrong3"}
        self.assertEqual(display_choices(None, values), values)
        self.assertEqual(display_key(None, "B"), "B")

    def test_reshuffle_changes_from_preceding_order_and_is_reversible(self):
        for previous in (None, "ABCD", "BCDA", "DCBA"):
            for _ in range(40):
                order = new_choice_order(previous)
                self.assertEqual(set(order), set("ABCD"))
                self.assertNotEqual(order, previous or "ABCD")
                for display in "ABCD":
                    canonical = canonical_key(order, display)
                    self.assertEqual(display_key(order, canonical), display)

    def test_text_scores_and_correct_key_follow_same_permutation(self):
        order = "BCDA"
        choices = {"A": "wrong1", "B": "correct", "C": "wrong2", "D": "wrong3"}
        scores = {"A": 12, "B": 90, "C": 43, "D": 55}
        self.assertEqual(display_choices(order, choices), {
            "A": "correct", "B": "wrong2", "C": "wrong3", "D": "wrong1",
        })
        self.assertEqual(display_scores(order, scores), {
            "A": 90, "B": 43, "C": 55, "D": 12,
        })
        self.assertEqual(display_key(order, "B"), "A")
        self.assertIsNone(display_scores(order, None))

    def test_corrupt_permutations_fail_closed(self):
        for order in ("AAAA", "ABCDX", "ABC", "ABCE"):
            with self.subTest(order=order), self.assertRaises(ValueError):
                display_choices(order, dict(zip("ABCD", range(4))))


if __name__ == "__main__":
    unittest.main()
