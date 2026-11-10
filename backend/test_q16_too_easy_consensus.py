"""Regression tests for Q16 subjective difficulty consensus.

Run from backend: python -m unittest test_q16_too_easy_consensus -v
"""
import unittest

from tagging.quality_skill.arbiter import ArbitrationVerdict, is_suitable


def verdict(difficulty="appropriate", *, premise="valid", answer_leak=False,
            choices=None):
    return ArbitrationVerdict(
        premise=premise,
        choices=choices or {"A": "yes", "B": "no", "C": "no", "D": "no"},
        difficulty=difficulty, answer_leak=answer_leak,
        stem_claims=[], rationale="Independent review complete.",
    )


class Q16TooEasyConsensusTests(unittest.TestCase):
    def decide(self, reviewer, terra, **kwargs):
        return is_suitable(verdict(terra, **kwargs),
                           correct_answer="A", bonus=True,
                           primary_difficulty=reviewer)

    def test_both_too_easy_rejected(self):
        self.assertEqual(self.decide("too_easy", "too_easy"),
                         (False, "bonus_too_easy"))

    def test_only_reviewer_too_easy_is_accepted(self):
        self.assertEqual(self.decide("too_easy", "appropriate"),
                         (True, "accepted"))

    def test_only_terra_too_easy_is_accepted(self):
        self.assertEqual(self.decide("appropriate", "too_easy"),
                         (True, "accepted"))

    def test_neither_too_easy_is_accepted(self):
        self.assertEqual(self.decide("appropriate", "appropriate"),
                         (True, "accepted"))

    def test_legacy_call_remains_conservative(self):
        self.assertEqual(is_suitable(verdict("too_easy"),
                                     correct_answer="A", bonus=True),
                         (False, "bonus_too_easy"))

    def test_false_premise_cannot_be_rescued(self):
        self.assertEqual(self.decide("appropriate", "too_easy",
                                     premise="invalid"),
                         (False, "premise_invalid"))

    def test_answer_leak_cannot_be_rescued(self):
        self.assertEqual(self.decide("appropriate", "too_easy",
                                     answer_leak=True),
                         (False, "answer_leak"))

    def test_wrong_key_cannot_be_rescued(self):
        self.assertEqual(self.decide("appropriate", "too_easy",
                                     choices={"A": "no", "B": "yes",
                                              "C": "no", "D": "no"}),
                         (False, "options_not_unique"))

    def test_non_bonus_ignores_too_easy_consensus(self):
        self.assertEqual(
            is_suitable(verdict("too_easy"), correct_answer="A", bonus=False,
                        primary_difficulty="too_easy"),
            (True, "accepted"),
        )


if __name__ == "__main__":
    unittest.main()
