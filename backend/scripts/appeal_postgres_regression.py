"""Real PostgreSQL smoke regression for appeal ledger and game invariants.

CI ONLY: expects a disposable DATABASE_URL; creates short-lived fixture rows.
Run after 'alembic upgrade head' against a test database.
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from persistence.appeal_service import file_appeal, quote_appeal, settle_appeal
from persistence.database import engine
from persistence.gameplay import GameConflict
from persistence.models import (
    GameRun, Question, QuestionAttempt, QuestionAppeal, Topic,
    User, WalletTransaction,
)


def fixture(db: Session, *, number: int, losing: bool = False,
            expenses: int = 0, wager: bool = False):
    suffix = str(uuid4())[:10]
    user = User(username="Test" + suffix, username_key="appeal-" + suffix)
    topic = Topic(slug="appeal-test-" + suffix, labels={"en": "Appeal test"})
    db.add_all([user, topic])
    db.flush()
    run = GameRun(user_id=user.id, language="English",
                  status="lost" if losing else "main",
                  bonus_wager_accepted=wager)
    db.add(run)
    db.flush()
    question = Question(
        topic_id=topic.id, language="English",
        requested_difficulty=15 if number == 16 else number - 1,
        text="Which answer equals 4?", choices={
            "A": "3", "B": "4", "C": "5", "D": "6"},
        correct_key="B", fingerprint=uuid4().hex + uuid4().hex,
        status="approved", generator_provider="openai",
        generator_model="gpt-5.4", prompt_version="v2", schema_version="v1",
    )
    db.add(question)
    db.flush()
    attempt = QuestionAttempt(
        run_id=run.id, question_id=question.id,
        number=number, requested_difficulty=question.requested_difficulty,
        answered_at=datetime.now(timezone.utc) if losing else None,
        selected_key="A" if losing else None,
        is_correct=False if losing else None,
        appeal_expenses={"hint_bronze": expenses} if expenses else {},
    )
    db.add(attempt)
    db.add(WalletTransaction(
        user_id=user.id, run_id=run.id, idempotency_key="fixture:" + suffix,
        amount=8, reason=("star/gold/test-credit" if number >= 11 else
                "star/silver/test-credit" if number >= 6 else
                "star/bronze/test-credit")))
    if expenses:
        db.add(WalletTransaction(
            user_id=user.id, run_id=run.id, idempotency_key="fixture:hint:" + suffix,
            amount=-expenses, reason="star/bronze/hide-1"))
    db.commit()
    return user.id, run.id, question.id, attempt.id


def ledger_count(db: Session, appeal_id, contains: str) -> int:
    return int(db.scalar(select(func.count(WalletTransaction.id)).where(
        WalletTransaction.idempotency_key.like(f"appeal:{appeal_id}:%"),
        WalletTransaction.reason.like(f"%{contains}%"),
    )) or 0)


def run():
    with Session(engine()) as db:
        uid, run_id, qid, attempt_id = fixture(db, number=3, expenses=2)
        quote = quote_appeal(db, run_id=run_id, user_id=uid, question_id=qid)
        assert quote["deposit_tier"] == "bronze"
        assert quote["filing_fee_bronze"] == 0
        request_id = uuid4()
        appeal = file_appeal(
            db, user_id=uid, run_id=run_id, question_id=qid,
            request_id=request_id, reasons=["ambiguous"],
            comment="two readings", source_links=[],
            confirm_nonrefundable_fee=False, expected_fee_bronze=0)
        aid = appeal.id
        assert db.get(Question, qid).status == "quarantined"
        assert db.get(QuestionAttempt, attempt_id).superseded_at is not None
        assert set(appeal.question_snapshot["answers"]) == set("ABCD")
        again = file_appeal(
            db, user_id=uid, run_id=run_id, question_id=qid,
            request_id=request_id, reasons=["ambiguous"],
            comment="two readings", source_links=[],
            confirm_nonrefundable_fee=False, expected_fee_bronze=0)
        assert again.id == aid
        assert ledger_count(db, aid, "appeal-deposit") == 1
        try:
            file_appeal(
                db, user_id=uid, run_id=run_id, question_id=qid,
                request_id=uuid4(), reasons=["ambiguous"],
                comment="two readings", source_links=[],
                confirm_nonrefundable_fee=False, expected_fee_bronze=0)
        except GameConflict:
            db.rollback()
        else:
            raise AssertionError("Duplicate complaint accepted")
        appeal = db.get(QuestionAppeal, aid)
        settle_appeal(db, appeal=appeal, decision="upheld",
                      verdict_en="Question was ambiguous",
                      verdict_localized="Question was ambiguous",
                      verdict_data={"judge_model": "test"})
        db.commit()
        # Idempotent callback must not credit a second time.
        settle_appeal(db, appeal=appeal, decision="upheld")
        db.commit()
        assert db.get(Question, qid).status == "rejected"
        assert ledger_count(db, aid, "appeal-deposit-refund") == 1
        assert ledger_count(db, aid, "appeal-hint-refund") == 1
        assert ledger_count(db, aid, "appeal-compensation") == 0
        assert appeal.verdict_data["expenses_refunded"][0]["amount"] == 2

        uid, run_id, qid, _ = fixture(db, number=16, losing=True, wager=True)
        quote = quote_appeal(db, run_id=run_id, user_id=uid, question_id=qid)
        assert quote["deposit_tier"] == "gold"
        appeal = file_appeal(
            db, user_id=uid, run_id=run_id, question_id=qid,
            request_id=uuid4(), reasons=["incorrect_key"],
            comment="incorrect answer", source_links=[],
            confirm_nonrefundable_fee=False, expected_fee_bronze=0)
        assert db.get(GameRun, run_id).status == "lost"
        assert db.get(QuestionAttempt, appeal.attempt_id).superseded_at is None
        settle_appeal(db, appeal=appeal, decision="upheld",
                      verdict_en="Wrong key", verdict_localized="Wrong key")
        db.commit()
        assert ledger_count(db, appeal.id, "appeal-compensation") == 1
        assert ledger_count(db, appeal.id, "appeal-bonus-wager-refund") == 1
        assert ledger_count(db, appeal.id, "appeal-deposit-refund") == 1
        assert db.get(GameRun, run_id).status == "lost"
        resolved_appeal_id = appeal.id
        resolved_question_id = qid
        resolved_username = db.get(User, uid).username

        uid, run_id, qid, _ = fixture(db, number=7)
        appeal = file_appeal(
            db, user_id=uid, run_id=run_id, question_id=qid,
            request_id=uuid4(), reasons=[], comment="technical case",
            source_links=[], confirm_nonrefundable_fee=False,
            expected_fee_bronze=0)
        settle_appeal(db, appeal=appeal, decision="technical_unresolved",
                      verdict_en="Technical", verdict_localized="Technical")
        db.commit()
        assert ledger_count(db, appeal.id, "appeal-deposit-refund") == 1
        assert ledger_count(db, appeal.id, "appeal-compensation") == 0
        assert db.get(GameRun, run_id).status == "main"
    # Scope-limited test-data reset keeps the wallet ledger auditable.
    from scripts.cleanup_test_appeals import cleanup
    with Session(engine()) as db:
        uid, run_id, qid, _ = fixture(db, number=2)
        username = db.get(User, uid).username
        appeal = file_appeal(
            db, user_id=uid, run_id=run_id, question_id=qid,
            request_id=uuid4(), reasons=["ambiguous"],
            comment="test cleanup", source_links=[],
            confirm_nonrefundable_fee=False, expected_fee_bronze=0)
        aid = appeal.id
        assert db.get(Question, qid).status == "quarantined"
    dry = cleanup(username=username, ids=[aid])
    assert len(dry) == 1 and dry[0]["status"] == "pending"
    with Session(engine()) as db:
        assert db.get(QuestionAppeal, aid) is not None
    applied = cleanup(username=username, ids=[aid], apply=True)
    assert len(applied) == 1
    with Session(engine()) as db:
        assert db.get(QuestionAppeal, aid) is None
        assert db.get(Question, qid).status == "approved"
        assert ledger_count(db, aid, "appeal-test-cleanup") == 1
    # A completed Q16 appeal includes platinum compensation and a separate
    # gold wager refund. Test cleanup must reverse BOTH without altering the
    # originally lost game or removing ledger history.
    done = cleanup(username=resolved_username, ids=[resolved_appeal_id])
    assert done[0]["reverse_star_movements"]["platinum"] == -1
    assert done[0]["reverse_star_movements"]["gold"] == -1
    cleanup(username=resolved_username, ids=[resolved_appeal_id], apply=True)
    with Session(engine()) as db:
        assert db.get(QuestionAppeal, resolved_appeal_id) is None
        assert db.get(Question, resolved_question_id).status == "approved"
        assert db.scalar(select(WalletTransaction.id).where(
            WalletTransaction.idempotency_key ==
            f"appeal:{resolved_appeal_id}:test-cleanup-platinum")) is not None
    print("PostgreSQL appeals regression: filing, settlement, resolved/queued cleanup & refunds OK")


if __name__ == "__main__":
    run()
