import unittest
from datetime import datetime, timezone

from persistence.appeal_policy import (
    appeal_fee_bronze, automatic_uphold, compensation_allowed, month_bounds,
    permanent_block, stake_refund_required, stake_tier, validate_reasons,
)


class AppealPolicyTests(unittest.TestCase):
    def test_fee_doubling(self):
        self.assertEqual([appeal_fee_bronze(i) for i in range(10)],
                         [0, 0, 0, 0, 0, 1, 2, 4, 8, 16])

    def test_tiers(self):
        self.assertEqual([stake_tier(i) for i in (1, 5, 6, 10, 11, 16)],
                         ["bronze", "bronze", "silver", "silver", "gold", "gold"])
        with self.assertRaises(ValueError):
            stake_tier(17)

    def test_pre_answer_cannot_allege_wrong_marked_key(self):
        with self.assertRaises(ValueError):
            validate_reasons(["incorrect_key"], before_answer=True)
        self.assertEqual(validate_reasons(["incorrect_key"], before_answer=False),
                         ("incorrect_key",))

    def test_permanent_blocks_are_distinct(self):
        self.assertIsNone(permanent_block(distinct_players=2, upheld=0))
        self.assertEqual(permanent_block(distinct_players=3, upheld=0), "complaint_threshold")
        self.assertEqual(permanent_block(distinct_players=1, upheld=1), "confirmed_error")
        self.assertTrue(automatic_uphold(2))
        self.assertFalse(automatic_uphold(1))

    def test_compensation_is_exact_question(self):
        self.assertFalse(compensation_allowed(lost_question_id=None, appealed_question_id="q9"))
        self.assertFalse(compensation_allowed(lost_question_id="q12", appealed_question_id="q9"))
        self.assertTrue(compensation_allowed(lost_question_id="q9", appealed_question_id="q9"))
        self.assertTrue(stake_refund_required(upheld=True))
        self.assertTrue(stake_refund_required(upheld=False, technical_unresolved=True))
        self.assertFalse(stake_refund_required(upheld=False))

    def test_calendar_month_is_utc(self):
        start, end = month_bounds(datetime(2026, 12, 15, tzinfo=timezone.utc))
        self.assertEqual((start.month, end.year, end.month), (12, 2027, 1))


if __name__ == "__main__":
    unittest.main()
