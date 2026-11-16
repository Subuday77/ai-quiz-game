"""Regression tests for the agreed refund of monthly appeal capacity.

These tests use a disposable SQLite database. They never rewrite real
PostgreSQL history or reverse any paid filing-fee transactions.
"""
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from persistence.appeal_service import _filings_this_month
from persistence.appeal_qa_quota import allowance
from persistence.appeal_routes import monthly_appeal_allowance
from persistence.models import Base, Question, QuestionAppeal, Topic, User


class UpheldAppealQuotaTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:", poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        @event.listens_for(self.engine, "connect")
        def foreign_keys(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")
        Base.metadata.create_all(self.engine)
        self.addCleanup(self.engine.dispose)
        self.db = Session(self.engine, expire_on_commit=False)
        self.addCleanup(self.db.close)
        self.enterContext(patch.dict(os.environ, {
            "QUIZ_QA_APPEAL_CREDITS_ENABLED": "false",
        }))
        self.now = datetime.now(timezone.utc)
        self.player = User(username="AppealQuotaPlayer",
                           username_key="appeal_quota_player")
        self.other = User(username="AppealQuotaOther",
                          username_key="appeal_quota_other")
        self.topic = Topic(slug="appeal-quota-regression", labels={"en": "Test"})
        self.db.add_all([self.player, self.other, self.topic])
        self.db.flush()

    def add_appeal(self, status, *, user=None, when=None, fee=0):
        question = Question(
            topic_id=self.topic.id, language="English",
            requested_difficulty=0, text="Test question",
            choices={"A": "One", "B": "Two", "C": "Three", "D": "Four"},
            correct_key="A", fingerprint=uuid4().hex,
            status="approved", generator_provider="test",
            generator_model="offline", prompt_version="test",
            schema_version="v1",
        )
        self.db.add(question)
        self.db.flush()
        appeal = QuestionAppeal(
            user_id=(user or self.player).id,
            question_id=question.id,
            idempotency_key="quota:" + uuid4().hex,
            question_snapshot={"question": "Test question"},
            language="English", reasons=["ambiguous"], comment="",
            source_links=[], context={}, before_answer=True,
            question_number=1, deposit_tier="bronze",
            filing_fee_bronze=fee, status=status,
            submitted_at=when or self.now,
        )
        self.db.add(appeal)
        self.db.commit()
        return appeal

    def count(self, *, include_upheld=False):
        return _filings_this_month(
            self.db, user_id=self.player.id, now=self.now,
            include_upheld=include_upheld)

    def quota(self):
        return allowance(self.db, user_id=self.player.id,
                         now=self.now, submitted=self.count())

    def test_upheld_restores_free_slot_without_erasing_filing_history(self):
        appeal = self.add_appeal("pending")
        self.assertEqual(self.count(), 1)
        self.assertEqual(self.quota()["free_appeals_remaining"], 4)
        appeal.status = "upheld"
        self.db.commit()

        self.assertEqual(self.count(), 0)
        self.assertEqual(self.count(include_upheld=True), 1)
        self.assertEqual(self.quota()["free_appeals_remaining"], 5)
        self.assertEqual(self.quota()["filing_fee_bronze"], 0)

        response = monthly_appeal_allowance(user=self.player, db=self.db)
        self.assertEqual(response["free_remaining"], 5)
        self.assertEqual(response["filed_this_month"], 1)
        self.assertEqual(response["next_filing_fee_bronze"], 0)
        self.assertEqual(self.db.get(QuestionAppeal, appeal.id).status, "upheld")

    def test_upheld_paid_filing_restores_prior_fee_step_but_not_paid_fee(self):
        for _ in range(5):
            self.add_appeal("rejected")
        self.assertEqual(self.count(), 5)
        self.assertEqual(self.quota()["free_appeals_remaining"], 0)
        self.assertEqual(self.quota()["filing_fee_bronze"], 1)

        paid = self.add_appeal("pending", fee=1)
        self.assertEqual(self.count(), 6)
        self.assertEqual(self.quota()["filing_fee_bronze"], 2)
        paid.status = "upheld"
        self.db.commit()

        self.assertEqual(self.count(), 5)
        self.assertEqual(self.quota()["filing_fee_bronze"], 1)
        self.assertEqual(paid.filing_fee_bronze, 1)
        self.assertEqual(self.count(include_upheld=True), 6)

        self.add_appeal("rejected", fee=1)
        self.assertEqual(self.count(), 6)
        self.assertEqual(self.quota()["filing_fee_bronze"], 2)
        self.assertEqual(self.count(include_upheld=True), 7)

    def test_only_upheld_is_excluded_from_quota_across_all_statuses(self):
        for status in ("pending", "retry_pending", "rejected",
                       "technical_unresolved", "upheld"):
            self.add_appeal(status)
        self.assertEqual(self.count(), 4)
        self.assertEqual(self.count(include_upheld=True), 5)
        self.assertEqual(self.quota()["free_appeals_remaining"], 1)

        self.add_appeal("rejected", user=self.other)
        self.add_appeal("rejected", when=self.now - timedelta(days=40))
        self.assertEqual(self.count(), 4)
        self.assertEqual(self.count(include_upheld=True), 5)

    def test_later_upheld_verdict_is_reflected_without_duplicate_credits(self):
        pending = self.add_appeal("pending")
        self.assertEqual(self.count(), 1)
        pending.status = "upheld"
        self.db.commit()
        self.assertEqual(self.count(), 0)
        # The quota is a read-time projection, not a grant transaction:
        # repeat reads cannot create extra slots beyond the monthly total.
        self.assertEqual(self.quota()["free_appeals_remaining"], 5)
        self.assertEqual(self.quota()["free_appeals_remaining"], 5)


if __name__ == "__main__":
    unittest.main()
