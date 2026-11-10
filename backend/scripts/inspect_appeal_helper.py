"""Read-only audit of a completed appeal's helper-consultation compensation.

Run in PyCharm backend terminal, using the normal backend .env:
    python -m scripts.inspect_appeal_helper a1a97880-ea24-4669-af90-f6e16c33f1eb

Only SELECT statements. No model/API calls, DB writes, wallet changes,
personal details, or correct-answer disclosures.
"""
from __future__ import annotations

import argparse
import json
from uuid import UUID

from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv())

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from persistence.database import engine  # noqa: E402
from persistence.models import (  # noqa: E402
    PlatinumHelperCall, PlatinumHelperPurchase, QuestionAppeal,
    QuestionAttempt, WalletTransaction, StarAward,
)


def inspect(db: Session, appeal_id: UUID) -> dict:
    appeal = db.get(QuestionAppeal, appeal_id)
    if appeal is None:
        return {"error": "appeal_not_found", "appeal_id": str(appeal_id)}
    ctx = appeal.context or {}
    purchase = db.scalar(select(PlatinumHelperPurchase).where(
        PlatinumHelperPurchase.run_id == appeal.run_id,
        PlatinumHelperPurchase.user_id == appeal.user_id,
    ))
    snap_purchase = ctx.get("helper_purchase_id")
    snapshot_used = bool(ctx.get("helper_used_on_question"))
    calls = []
    reward_rows = []
    if purchase is not None:
        matches = db.execute(
            select(PlatinumHelperCall, QuestionAttempt)
            .join(QuestionAttempt,
                  PlatinumHelperCall.question_attempt_id == QuestionAttempt.id)
            .where(
                PlatinumHelperCall.purchase_id == purchase.id,
                QuestionAttempt.question_id == appeal.question_id,
            )
            .order_by(PlatinumHelperCall.created_at)
        ).all()
        calls = [
            {
                "status": call.status,
                "same_appeal_attempt": attempt.id == appeal.attempt_id,
                "created_before_appeal": call.created_at <= appeal.submitted_at
                if call.created_at and appeal.submitted_at else None,
            }
            for call, attempt in matches
        ]
        reward_rows = db.scalars(select(WalletTransaction).where(
            WalletTransaction.user_id == appeal.user_id,
            WalletTransaction.run_id == appeal.run_id,
            WalletTransaction.idempotency_key == f"helper:{purchase.id}:appeal-gold",
        )).all()
    # Show every credited gold amount in this game, to distinguish an appeal
    # reward from an unrelated award or payment.
    gold_credits = db.scalars(select(WalletTransaction).where(
        WalletTransaction.user_id == appeal.user_id,
        WalletTransaction.run_id == appeal.run_id,
        WalletTransaction.reason.like("star/gold/%"),
        WalletTransaction.amount > 0,
    ).order_by(WalletTransaction.created_at)).all()
    appeal_ledger = db.scalars(select(WalletTransaction).where(
        WalletTransaction.user_id == appeal.user_id,
        WalletTransaction.idempotency_key.like(f"appeal:{appeal.id}:%"),
    ).order_by(WalletTransaction.created_at)).all()
    gold_star_awards = db.scalars(select(StarAward).where(
        StarAward.user_id == appeal.user_id, StarAward.run_id == appeal.run_id,
        StarAward.tier == "gold",
    )).all()

    report = {
        "appeal_id": str(appeal.id),
        "status": appeal.status,
        "number": appeal.question_number,
        "filed_before_answer": appeal.before_answer,
        "filed_helper_snapshot": {
            "had_purchase_id": bool(snap_purchase),
            "purchase_matches_current": bool(purchase and str(purchase.id) == snap_purchase),
            "completed_call_on_question": snapshot_used,
        },
        "purchase_now": None if purchase is None else {
            "consultations_used": purchase.consultations_used,
            "refunded": purchase.refunded,
        },
        "calls_for_appealed_question": calls,
        "helper_reward_transactions": [
            {"amount": row.amount, "reason": row.reason}
            for row in reward_rows
        ],
        "recorded_refunds": (appeal.verdict_data or {}).get("expenses_refunded", []),
        "appeal_ledger": [
            {"amount": row.amount, "reason": row.reason}
            for row in appeal_ledger
        ],
        "other_gold_credits_same_game": [
            {"amount": row.amount, "reason": row.reason}
            for row in gold_credits
        ],
        "gold_star_awards_same_game": len(gold_star_awards),
    }
    # Determine only which layer needs investigation, not who is owed money.
    if not purchase or not snap_purchase:
        diagnosis = "missing_helper_purchase_in_appeal_snapshot"
    elif not snapshot_used:
        diagnosis = "no_completed_helper_call_at_appeal_filing"
    elif reward_rows and not any(
            r.get("reason") == "helper" for r in report["recorded_refunds"]):
        diagnosis = "reward_paid_but_omitted_from_current_appeal_receipt"
    elif reward_rows:
        diagnosis = "helper_reward_paid_and_recorded"
    elif purchase.refunded:
        diagnosis = "purchase_refunded_before_or_by_settlement"
    else:
        diagnosis = "helper_reward_not_issued_despite_completed_call_snapshot"
    report["diagnosis"] = diagnosis
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("appeal_id", type=UUID)
    args = parser.parse_args()
    with Session(engine()) as db:
        print(json.dumps(inspect(db, args.appeal_id),
                         ensure_ascii=False, indent=2, default=str))
        db.rollback()


if __name__ == "__main__":
    main()
