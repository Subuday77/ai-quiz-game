"""Only opted-in QA accounts receive extra free complaint filing quota."""
import os
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from persistence.appeal_qa_quota import allowance, extra_free_for_month
from persistence.appeal_policy import month_bounds


class AppealQaQuotaTests(unittest.TestCase):
    def setUp(self):
        self.db = MagicMock()
        self.user_id = uuid4()
        self.now = datetime(2026, 11, 9, 21, tzinfo=timezone.utc)

    def test_opt_out_never_reads_qa_table_and_keeps_original_policy(self):
        with patch.dict(os.environ, {"QUIZ_QA_APPEAL_CREDITS_ENABLED": "false"}):
            self.assertEqual(
                allowance(self.db, user_id=self.user_id, now=self.now, submitted=5),
                {"free_appeals_remaining": 0, "free_appeals_total": 5,
                 "free_appeals_refresh_at": "2026-12-01T00:00:00+00:00",
                 "filing_fee_bronze": 1},
            )
            self.db.get.assert_not_called()

    def test_grant_expands_free_total_and_only_defers_nonrefundable_fee(self):
        self.db.get.return_value = SimpleNamespace(extra_free=10)
        with patch.dict(os.environ, {"QUIZ_QA_APPEAL_CREDITS_ENABLED": "true"}):
            for used, free_remaining, fee in (
                (5, 10, 0), (14, 1, 0), (15, 0, 1), (16, 0, 2)
            ):
                with self.subTest(used=used):
                    info = allowance(self.db, user_id=self.user_id,
                                     now=self.now, submitted=used)
                    self.assertEqual(info["free_appeals_total"], 15)
                    self.assertEqual(info["free_appeals_remaining"], free_remaining)
                    self.assertEqual(info["filing_fee_bronze"], fee)
            first, _ = month_bounds(self.now)
            self.db.get.assert_any_call(
                __import__("persistence.models", fromlist=["AppealQaFreeQuota"]).AppealQaFreeQuota,
                (self.user_id, first),
            )

    def test_qa_grants_are_month_scoped_not_carried_over(self):
        self.db.get.side_effect = [
            SimpleNamespace(extra_free=3), None,
        ]
        dec = datetime(2026, 12, 1, tzinfo=timezone.utc)
        with patch.dict(os.environ, {"QUIZ_QA_APPEAL_CREDITS_ENABLED": "true"}):
            nov = allowance(self.db, user_id=self.user_id, now=self.now, submitted=0)
            december = allowance(self.db, user_id=self.user_id, now=dec, submitted=0)
        self.assertEqual(nov["free_appeals_total"], 8)
        self.assertEqual(december["free_appeals_total"], 5)
        self.assertEqual(december["free_appeals_refresh_at"],
                         "2027-01-01T00:00:00+00:00")

    def test_negative_count_rejected(self):
        with self.assertRaises(ValueError):
            allowance(self.db, user_id=self.user_id, now=self.now, submitted=-1)


if __name__ == "__main__":
    unittest.main()
