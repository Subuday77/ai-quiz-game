"""Only opted-in QA accounts receive extra free complaint filing quota."""
import os
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from persistence.appeal_qa_quota import allowance, extra_free_for_month
from persistence.appeal_policy import month_bounds
from persistence.models import AppealQaFreeQuota
from scripts.grant_test_appeals import grant


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
                AppealQaFreeQuota,
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

    def test_cli_dry_run_never_mutates_database(self):
        with (patch.dict(os.environ, {"QUIZ_QA_APPEAL_CREDITS_ENABLED": "true"}),
              patch("scripts.grant_test_appeals.engine"),
              patch("scripts.grant_test_appeals.Session") as session,
              patch("scripts.grant_test_appeals._filings_this_month", return_value=2)):
            db = session.return_value.__enter__.return_value
            db.scalar.return_value = SimpleNamespace(
                id=self.user_id, username="AI_Tester_1")
            db.get.return_value = None
            result = grant(username="AI_Tester_1", count=10, now=self.now)
            self.assertEqual(result["mode"], "DRY_RUN")
            self.assertEqual(result["free_remaining"], 13)
            self.assertEqual(result["free_total"], 15)
            db.add.assert_not_called()
            db.commit.assert_not_called()
            db.rollback.assert_called_once()

    def test_cli_apply_updates_one_scoped_month_only(self):
        with (patch.dict(os.environ, {"QUIZ_QA_APPEAL_CREDITS_ENABLED": "true"}),
              patch("scripts.grant_test_appeals.engine"),
              patch("scripts.grant_test_appeals.Session") as session,
              patch("scripts.grant_test_appeals.lock_user_stars") as lock,
              patch("scripts.grant_test_appeals._filings_this_month", return_value=3)):
            db = session.return_value.__enter__.return_value
            db.scalar.return_value = SimpleNamespace(
                id=self.user_id, username="AI_Tester_1")
            db.get.return_value = SimpleNamespace(extra_free=2)
            result = grant(username="AI_Tester_1", count=8, now=self.now, apply=True)
            lock.assert_called_once_with(db, self.user_id)
            self.assertEqual(result["new_qa_credits"], 10)
            self.assertEqual(result["free_remaining"], 12)
            self.assertEqual(db.get.return_value.extra_free, 10)
            db.commit.assert_called_once()
            db.add.assert_not_called()

    def test_cli_requires_explicit_opt_in_and_bounded_count(self):
        with patch.dict(os.environ, {"QUIZ_QA_APPEAL_CREDITS_ENABLED": "false"}):
            with self.assertRaisesRegex(RuntimeError, "QUIZ_QA_APPEAL_CREDITS_ENABLED"):
                grant(username="AI_Tester_1", count=10, now=self.now)
        with patch.dict(os.environ, {"QUIZ_QA_APPEAL_CREDITS_ENABLED": "true"}):
            with self.assertRaisesRegex(ValueError, "between 1 and 100"):
                grant(username="AI_Tester_1", count=1001, now=self.now)

    def test_negative_count_rejected(self):
        with self.assertRaises(ValueError):
            allowance(self.db, user_id=self.user_id, now=self.now, submitted=-1)


if __name__ == "__main__":
    unittest.main()
