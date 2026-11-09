"""Explicit, audit-safe cleanup of specific QA complaint records.

STOP FastAPI first. By default only a dry run is performed. Passing --apply
requires --confirm RESET-TEST-APPEALS and the exact owner + appeal IDs.
No blanket UPDATE/DELETE, no wiping wallet history, no unrelated players.

For each scoped complaint, create compensating ledger entries (including
the inverse of any payouts that a test Judge issued), then remove ONLY these
appeal/moderation records and restore the questions to approved. Refuse
mixed-player moderation, unknown ledger entries or helper-linked payouts.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from persistence.database import engine
from persistence.models import (
    User, Question, QuestionAppeal, QuestionModeration, WalletTransaction,
)
from persistence.star_economy import lock_user_stars, star_balance


def cleanup(*, username: str, ids: list[UUID], apply: bool = False) -> list[dict]:
    if not username or not ids or len(ids) != len(set(ids)):
        raise ValueError("A username and distinct explicit appeal IDs are required")
    with Session(engine()) as db:
        user = db.scalar(select(User).where(User.username == username))
        if user is None:
            raise ValueError("User not found")
        if apply:
            lock_user_stars(db, user.id)
        stmt = select(QuestionAppeal).where(
            QuestionAppeal.id.in_(ids), QuestionAppeal.user_id == user.id,
        ).order_by(QuestionAppeal.id)
        if apply:
            stmt = stmt.with_for_update()
        appeals = list(db.scalars(stmt))
        if len(appeals) != len(ids):
            raise ValueError("Some appeal IDs do not belong to this user")
        by_question = Counter(a.question_id for a in appeals)
        for question_id, count in sorted(by_question.items(), key=lambda x: str(x[0])):
            others = list(db.scalars(select(QuestionAppeal.id).where(
                QuestionAppeal.question_id == question_id,
                QuestionAppeal.id.notin_(ids),
            )))
            if others:
                raise ValueError(f"Question {question_id} has other player appeals; refuse")
            question = db.get(Question, question_id, with_for_update=apply)
            moderation = db.get(QuestionModeration, question_id, with_for_update=apply)
            if question is None or moderation is None or question.status not in (
                "quarantined", "approved", "rejected"
            ):
                raise ValueError(f"Question {question_id} has unexpected state")
            selected = [a for a in appeals if a.question_id == question_id]
            if (moderation.distinct_complainants != count or
                moderation.upheld_count != sum(a.status == "upheld" for a in selected) or
                moderation.rejected_count != sum(a.status == "rejected" for a in selected)):
                raise ValueError(f"Question {question_id} has mixed moderation history")
            if moderation.permanently_blocked != (
                moderation.upheld_count >= 1 or moderation.distinct_complainants >= 3
            ):
                raise ValueError(f"Question {question_id} has unknown permanent block")
        known_actions = {
            "deposit", "fee", "appeal-deposit-refund", "appeal-hint-refund",
            "appeal-compensation", "appeal-bonus-wager-refund",
        }
        totals: dict[str, int] = defaultdict(int)
        plan: list[dict] = []
        reversals: list[tuple[QuestionAppeal, str, int]] = []
        for appeal in appeals:
            refunded = (appeal.verdict_data or {}).get("expenses_refunded", [])
            if any(item.get("reason") in ("helper", "unused_helper") for item in refunded):
                raise ValueError(f"Appeal {appeal.id} has helper-linked credits; manual audit required")
            prefix = f"appeal:{appeal.id}:"
            transactions = list(db.scalars(select(WalletTransaction).where(
                WalletTransaction.idempotency_key.like(prefix + "%"),
            )))
            seen = set()
            per_tier: dict[str, int] = defaultdict(int)
            for tx in transactions:
                action = tx.idempotency_key[len(prefix):]
                if (action not in known_actions or action in seen or
                    tx.user_id != user.id or tx.run_id != appeal.run_id or
                    not tx.reason.startswith("star/")):
                    raise ValueError(f"Unknown or duplicate transaction: {tx.idempotency_key}")
                seen.add(action)
                tier = tx.reason.split("/")[1]
                if tier not in ("bronze", "silver", "gold", "platinum"):
                    raise ValueError("Unknown star tier in ledger")
                per_tier[tier] += tx.amount
            if "deposit" not in seen:
                raise ValueError(f"No appeal deposit for {appeal.id}")
            deposit = next(t for t in transactions if t.idempotency_key == prefix + "deposit")
            if deposit.amount != -1 or deposit.reason != (
                    f"star/{appeal.deposit_tier}/appeal-deposit"):
                raise ValueError(f"Invalid deposit for {appeal.id}")
            if bool(appeal.filing_fee_bronze) != ("fee" in seen):
                raise ValueError(f"Filing fee ledger mismatch for {appeal.id}")
            if appeal.filing_fee_bronze:
                fee = next(t for t in transactions if t.idempotency_key == prefix + "fee")
                if fee.amount != -appeal.filing_fee_bronze or fee.reason != "star/bronze/appeal-fee":
                    raise ValueError(f"Invalid filing fee for {appeal.id}")
            if bool(appeal.deposit_refunded_at) != ("appeal-deposit-refund" in seen):
                raise ValueError(f"Deposit refund ledger mismatch for {appeal.id}")
            if bool(appeal.compensated_at) != ("appeal-compensation" in seen):
                raise ValueError(f"Compensation ledger mismatch for {appeal.id}")
            if bool(appeal.bonus_wager_refunded_at) != ("appeal-bonus-wager-refund" in seen):
                raise ValueError(f"Bonus wager ledger mismatch for {appeal.id}")
            for tier, net in per_tier.items():
                if net:
                    reverse = -net
                    totals[tier] += reverse
                    reversals.append((appeal, tier, reverse))
            plan.append({
                "appeal_id": str(appeal.id),
                "question_id": str(appeal.question_id),
                "status": appeal.status,
                "reverse_star_movements": {tier: -net for tier, net in per_tier.items() if net},
                "question_restore": "approved",
            })
        # Ensure negative reversals do not make the actual star account insolvent.
        for tier, delta in totals.items():
            if star_balance(db, user.id, tier) + delta < 0:
                raise ValueError(f"Insufficient {tier} balance to reverse QA payouts")
        if apply:
            for appeal, tier, amount in reversals:
                db.add(WalletTransaction(
                    user_id=user.id, run_id=appeal.run_id,
                    idempotency_key=f"appeal:{appeal.id}:test-cleanup-{tier}",
                    amount=amount, reason=f"star/{tier}/appeal-test-cleanup",
                ))
            for appeal in appeals:
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
    parser = argparse.ArgumentParser(description="Audit-first scoped complaint QA cleanup")
    parser.add_argument("--username", required=True)
    parser.add_argument("--appeal-id", required=True, action="append", type=UUID)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()
    if args.apply and args.confirm != "RESET-TEST-APPEALS":
        parser.error("To apply, pass --confirm RESET-TEST-APPEALS after dry-run")
    rows = cleanup(username=args.username, ids=args.appeal_id, apply=args.apply)
    print(json.dumps({"mode": "APPLIED" if args.apply else "DRY_RUN",
                      "count": len(rows), "items": rows}, indent=2))
