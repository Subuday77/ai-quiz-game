"""Explicit, scoped cleanup of test appeals without corrupting the star ledger.

By default this prints a plan only. STOP FastAPI before running --apply,
because no live model review may race with removal of test records.

Example (backend directory):
 python -m scripts.cleanup_test_appeals --username AI_Tester_1 \
   --appeal-id ed220a73-41df-474e-8a3b-1d9d23297dce \
   --appeal-id ada6d5fd-0b3d-4e2a-8631-0bb091c6b856

Apply ONLY after inspecting dry-run output, with server stopped:
 python -m scripts.cleanup_test_appeals ... --apply --confirm RESET-TEST-APPEALS

No blanket UPDATE questions or DELETE wallet transactions: compensate spent
test deposits with separate idempotent credits for an auditable zero net cost.
ONLY unresolved, zero-fee pending cases with no other complaints on their
questions are eligible for automatic cleanup. Other statuses are rejected.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from persistence.database import engine
from persistence.models import (
    User, Question, QuestionAppeal, QuestionModeration, WalletTransaction,
)
from persistence.star_economy import lock_user_stars


def cleanup(*, username: str, ids: list[UUID], apply: bool = False) -> list[dict]:
    if not username or not ids or len(ids) != len(set(ids)):
        raise ValueError("A username and distinct explicit appeal IDs are required")
    with Session(engine()) as db:
        user = db.scalar(select(User).where(User.username == username))
        if user is None:
            raise ValueError("User not found")
        if apply:
            lock_user_stars(db, user.id)
        appeals = list(db.scalars(select(QuestionAppeal).where(
            QuestionAppeal.id.in_(ids), QuestionAppeal.user_id == user.id,
        ).order_by(QuestionAppeal.id).with_for_update() if apply
            else select(QuestionAppeal).where(
                QuestionAppeal.id.in_(ids), QuestionAppeal.user_id == user.id
            ).order_by(QuestionAppeal.id)))
        if len(appeals) != len(ids):
            raise ValueError("Some appeal IDs do not belong to this user")
        by_question = Counter(a.question_id for a in appeals)
        plan: list[dict] = []
        for question_id, requested_count in sorted(by_question.items(), key=lambda x: str(x[0])):
            others = list(db.scalars(select(QuestionAppeal.id).where(
                QuestionAppeal.question_id == question_id,
                QuestionAppeal.id.notin_(ids),
            )))
            if others:
                raise ValueError(f"Question {question_id} has other appeals; refuse")
            question = db.get(Question, question_id, with_for_update=apply)
            moderation = db.get(QuestionModeration, question_id, with_for_update=apply)
            if question is None or moderation is None or question.status != "quarantined":
                raise ValueError(f"Question {question_id} is not safely quarantined")
            if (moderation.permanently_blocked or moderation.upheld_count != 0 or
                moderation.rejected_count != 0 or
                moderation.distinct_complainants != requested_count):
                raise ValueError(f"Question {question_id} has mixed moderation history")
        for appeal in appeals:
            if appeal.status not in ("pending", "retry_pending"):
                raise ValueError("Only unresolved test complaints may be cleaned safely")
            if appeal.filing_fee_bronze != 0 or appeal.deposit_refunded_at or appeal.compensated_at:
                raise ValueError("Paid filing fees or existing payouts require manual audit")
            if appeal.bonus_wager_refunded_at or (appeal.verdict_data or {}).get("expenses_refunded"):
                raise ValueError("Complicated financial history requires manual audit")
            deposit_key = f"appeal:{appeal.id}:deposit"
            original = db.scalar(select(WalletTransaction).where(
                WalletTransaction.idempotency_key == deposit_key,
                WalletTransaction.user_id == user.id,
            ))
            if (original is None or original.amount != -1 or
                original.reason != f"star/{appeal.deposit_tier}/appeal-deposit"):
                raise ValueError(f"Deposit ledger mismatch for appeal {appeal.id}")
            transactions = list(db.scalars(select(WalletTransaction).where(
                WalletTransaction.idempotency_key.like(f"appeal:{appeal.id}:%")
            )))
            if len(transactions) != 1:
                raise ValueError(f"Unexpected extra ledger entries for appeal {appeal.id}")
            plan.append({"appeal_id": str(appeal.id), "question_id": str(appeal.question_id),
                         "deposit_return": f"1 {appeal.deposit_tier}",
                         "question_restore": "approved", "status": appeal.status})
        if apply:
            for appeal in appeals:
                db.add(WalletTransaction(
                    user_id=user.id, run_id=appeal.run_id,
                    idempotency_key=f"appeal:{appeal.id}:test-cleanup-deposit-return",
                    amount=1, reason=f"star/{appeal.deposit_tier}/appeal-test-cleanup",
                ))
                db.delete(appeal)
            db.flush()
            for question_id in by_question:
                question = db.get(Question, question_id, with_for_update=True)
                moderation = db.get(QuestionModeration, question_id, with_for_update=True)
                question.status = "approved"
                db.delete(moderation)
            db.commit()
        else:
            db.rollback()
        return plan


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Audit-first, scoped complaint QA cleanup")
    parser.add_argument("--username", required=True)
    parser.add_argument("--appeal-id", required=True, action="append", type=UUID)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()
    if args.apply and args.confirm != "RESET-TEST-APPEALS":
        parser.error("To apply, pass --confirm RESET-TEST-APPEALS after dry-run")
    import json
    rows = cleanup(username=args.username, ids=args.appeal_id, apply=args.apply)
    print(json.dumps({"mode": "APPLIED" if args.apply else "DRY_RUN",
                      "count": len(rows), "items": rows}, indent=2))
