"""Stale-game cleanup never deletes history or silently forfeits paid perks."""
import unittest
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from persistence.cleanup_stale_rounds import cleanup
from persistence.models import (
    Base, GameRun, PlatinumHelperPurchase, User, WalletTransaction,
)


class CleanupStaleRoundsTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine, autoflush=False, expire_on_commit=False)
        self.user = User(username="Alice", username_key="alice")
        self.other = User(username="Bob", username_key="bob")
        self.db.add_all([self.user, self.other])
        self.db.flush()
        old = datetime.now(timezone.utc) - timedelta(days=5)
        self.unpaid = GameRun(user_id=self.user.id, status="main", language="English",
                              started_at=old)
        self.paid = GameRun(user_id=self.user.id, status="main", language="Russian",
                            started_at=old)
        self.recent = GameRun(user_id=self.user.id, status="main", language="German")
        self.another_user = GameRun(user_id=self.other.id, status="main",
                                    language="Spanish", started_at=old)
        self.db.add_all([self.unpaid, self.paid, self.recent, self.another_user])
        self.db.flush()
        self.db.add(PlatinumHelperPurchase(
            run_id=self.paid.id, user_id=self.user.id, price=1,
            profession="Physicist", profile={"name": "Protected"},
        ))
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_dry_run_no_mutation_and_apply_preserves_paid_round(self):
        result = cleanup(self.db, username="Alice", older_than_days=2, apply=False)
        self.assertFalse(result["applied"])
        self.assertEqual([x["game_id"] for x in result["archivable"]],
                         [str(self.unpaid.id)])
        self.assertEqual([x["game_id"] for x in result["protected"]],
                         [str(self.paid.id)])
        self.assertEqual(self.unpaid.status, "main")

        result = cleanup(self.db, username="Alice", older_than_days=2, apply=True)
        self.assertTrue(result["applied"])
        self.assertEqual(self.unpaid.status, "abandoned")
        self.assertIsNotNone(self.unpaid.finished_at)
        self.assertEqual(self.paid.status, "main")
        self.assertEqual(self.recent.status, "main")
        self.assertEqual(self.another_user.status, "main")
        self.assertEqual(self.db.scalar(select(func.count(GameRun.id))), 4)

    def test_other_star_spending_is_protected_even_without_helper(self):
        self.db.add(WalletTransaction(
            user_id=self.user.id, run_id=self.unpaid.id,
            idempotency_key="spent-silver-perk", amount=-5,
            reason="star/silver/exclude-categories",
        ))
        self.db.commit()
        result = cleanup(self.db, username="Alice", older_than_days=2, apply=True)
        self.assertEqual(result["archivable"], [])
        self.assertEqual(len(result["protected"]), 2)
        self.assertEqual(self.unpaid.status, "main")

    def test_reject_unknown_user_and_invalid_age(self):
        with self.assertRaises(ValueError):
            cleanup(self.db, username="Charlie", older_than_days=2, apply=False)
        with self.assertRaises(ValueError):
            cleanup(self.db, username="Alice", older_than_days=0, apply=False)


if __name__ == "__main__":
    unittest.main()
