"""Offline, cost-free tests for the star-perk rules."""
import random
import unittest

from star_perks import (
    PerkUnavailable, category_change_price, choose_to_hide, exchange_quote,
    normalized_plausibility, remove_price, validate_allowed_categories,
)


class ExchangeTests(unittest.TestCase):
    def test_bidirectional_exchange_and_no_rounding(self):
        self.assertEqual(exchange_quote("bronze", "silver", 10), 1)
        self.assertEqual(exchange_quote("silver", "bronze", 1), 10)
        self.assertEqual(exchange_quote("silver", "gold", 10), 1)
        self.assertEqual(exchange_quote("gold", "silver", 1), 10)
        self.assertEqual(exchange_quote("bronze", "gold", 100), 1)
        self.assertEqual(exchange_quote("gold", "bronze", 1), 100)
        for amount in (0, -10, 9, True):
            with self.assertRaises(PerkUnavailable):
                exchange_quote("bronze", "silver", amount)

    def test_platinum_cannot_exchange(self):
        for a, b in (("platinum", "gold"), ("bronze", "platinum")):
            with self.assertRaises(PerkUnavailable):
                exchange_quote(a, b, 10)


class DistractorTests(unittest.TestCase):
    def test_prices_and_two_step_discount(self):
        self.assertEqual(remove_price(0, 1), 2)
        self.assertEqual(remove_price(0, 2), 3)
        self.assertEqual(remove_price(1, 1), 2)
        for state in ((1, 2), (2, 1), (0, 3)):
            with self.assertRaises(PerkUnavailable):
                remove_price(*state)

    def test_low_scores_first_true_key_never_hidden(self):
        scores = {"A": 20, "B": 100, "C": 40, "D": 50}
        self.assertEqual(choose_to_hide("B", scores, count=2, rng=random.Random(1)), ("A", "C"))
        self.assertEqual(choose_to_hide("B", scores, ("A",), rng=random.Random(1)), ("C",))
        self.assertEqual(normalized_plausibility("B", {"B": 8})["B"], 100)

    def test_tied_lowest_are_randomized(self):
        scores = {"A": 20, "B": 100, "C": 20, "D": 50}
        choices = {choose_to_hide("B", scores, rng=random.Random(seed))[0] for seed in range(50)}
        self.assertEqual(choices, {"A", "C"})

    def test_old_question_without_scores_uses_neutral_fallback(self):
        self.assertEqual(normalized_plausibility("C", None),
                         {"A": 50, "B": 50, "C": 100, "D": 50})
        self.assertNotIn("C", choose_to_hide("C", None, count=2))

    def test_invalid_or_fake_hidden_state_rejected(self):
        for bad in (("B",), ("A", "A")):
            with self.assertRaises(PerkUnavailable):
                choose_to_hide("B", None, bad)


class CategoryTests(unittest.TestCase):
    def test_paid_exclusions_leave_at_least_sixteen(self):
        categories = [f"topic-{i}" for i in range(20)]
        self.assertEqual(len(validate_allowed_categories(categories, categories[:4])), 16)
        with self.assertRaises(PerkUnavailable):
            validate_allowed_categories(categories, categories[:5])
        with self.assertRaises(PerkUnavailable):
            validate_allowed_categories(categories, ["unknown"])

    def test_shared_and_manual_caps(self):
        with self.assertRaises(PerkUnavailable):
            category_change_price(allowed_count=16, changes_used=0, manual_used=0, manual=False)
        self.assertEqual(category_change_price(allowed_count=20, changes_used=0,
                                                manual_used=0, manual=False), 1)
        self.assertEqual(category_change_price(allowed_count=20, changes_used=1,
                                                manual_used=0, manual=True), 2)
        with self.assertRaises(PerkUnavailable):
            category_change_price(allowed_count=20, changes_used=1, manual_used=1, manual=True)
        with self.assertRaises(PerkUnavailable):
            category_change_price(allowed_count=20, changes_used=2, manual_used=1, manual=False)


if __name__ == "__main__":
    unittest.main()
