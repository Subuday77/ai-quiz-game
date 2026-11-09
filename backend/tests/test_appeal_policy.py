"""Regression tests for the user-approved appeal economy and UI reasons."""
import unittest
from datetime import datetime, timedelta, timezone

from persistence.appeal_policy import (
    ALL_REASONS, BONUS_APPEAL_DEPOSIT_TIER, BONUS_WAGER_TIER,
    MAX_IMMEDIATE_JUDGE_ATTEMPTS, appeal_deposit_tier, appeal_fee_bronze,
    bonus_wager_refund, compensation_tier, loss_compensation_allowed,
    month_bounds, must_close_technical, permanent_block_reason,
    refund_deposit, should_auto_uphold, validate_complaint,
)


class AppealPolicyTests(unittest.TestCase):
    def test_monthly_fees_are_nonrefundable_and_progressively_doubled(self):
        self.assertEqual([appeal_fee_bronze(i) for i in range(10)],
                         [0, 0, 0, 0, 0, 1, 2, 4, 8, 16])

    def test_deposit_ranges_and_bonus_wager_are_independent(self):
        self.assertEqual([appeal_deposit_tier(i)
                          for i in (1, 5, 6, 10, 11, 15, 16)],
                         ["bronze", "bronze", "silver", "silver",
                          "gold", "gold", "gold"])
        self.assertEqual(BONUS_WAGER_TIER, "gold")
        self.assertEqual(BONUS_APPEAL_DEPOSIT_TIER, "gold")
        self.assertEqual(compensation_tier(16), "platinum")
        with self.assertRaises(ValueError):
            appeal_deposit_tier(17)

    def test_only_five_checkboxes_remain(self):
        self.assertEqual(ALL_REASONS, {
            "multiple_correct", "no_correct", "incorrect_key",
            "ambiguous", "answer_leak",
        })
        for removed in ("bad_translation", "difficulty_mismatch"):
            with self.assertRaises(ValueError):
                validate_complaint([removed], None, before_answer=False)

    def test_incorrect_key_only_after_answer(self):
        with self.assertRaises(ValueError):
            validate_complaint(["incorrect_key"], "", before_answer=True)
        self.assertEqual(validate_complaint(["incorrect_key"], "", before_answer=False),
                         (("incorrect_key",), ""))

    def test_free_text_alone_can_explain_difficulty_or_translation(self):
        self.assertEqual(validate_complaint([], "  Question is too hard  ", before_answer=True),
                         ((), "Question is too hard"))
        with self.assertRaises(ValueError):
            validate_complaint([], " ", before_answer=True)
        with self.assertRaises(ValueError):
            validate_complaint(["ambiguous", "ambiguous"], "", before_answer=True)
        with self.assertRaises(ValueError):
            validate_complaint(["ambiguous"], "x" * 1001, before_answer=True)

    def test_moderation_thresholds(self):
        self.assertIsNone(permanent_block_reason(distinct_players=2, upheld=0))
        self.assertEqual(permanent_block_reason(distinct_players=3, upheld=0),
                         "complaint_threshold")
        self.assertEqual(permanent_block_reason(distinct_players=1, upheld=1),
                         "confirmed_error")
        self.assertFalse(should_auto_uphold(1))
        self.assertTrue(should_auto_uphold(2))

    def test_loss_and_wager_refund_are_exact_question_only(self):
        self.assertFalse(loss_compensation_allowed(
            lost_question_id="q7", appealed_question_id="q16"))
        self.assertTrue(loss_compensation_allowed(
            lost_question_id="q16", appealed_question_id="q16"))
        self.assertTrue(bonus_wager_refund(
            question_number=16, upheld=True, caused_loss=True))
        self.assertFalse(bonus_wager_refund(
            question_number=16, upheld=True, caused_loss=False))
        self.assertFalse(bonus_wager_refund(
            question_number=15, upheld=True, caused_loss=True))
        self.assertTrue(refund_deposit(upheld=False, technical_unresolved=True))
        self.assertFalse(refund_deposit(upheld=False))
        self.assertEqual(MAX_IMMEDIATE_JUDGE_ATTEMPTS, 3)

    def test_seven_calendar_day_deadline(self):
        started = datetime(2026, 11, 9, tzinfo=timezone.utc)
        self.assertFalse(must_close_technical(
            started, started + timedelta(days=7, seconds=-1)))
        self.assertTrue(must_close_technical(
            started, started + timedelta(days=7)))

    def test_utc_month_reset(self):
        start, end = month_bounds(datetime(2026, 12, 31, tzinfo=timezone.utc))
        self.assertEqual((start.month, end.year, end.month), (12, 2027, 1))


if __name__ == "__main__":
    unittest.main()
