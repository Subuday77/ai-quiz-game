"""Restore a missing *receipt entry* for gold already paid to a helper.

Historical issue: SQLAlchemy JSON list was mutated in place after a bronze
hint refund was flushed. The gold wallet entry was committed, but the second
receipt item was not. This command does NOT credit any wallet/star balance.

Run from backend in PyCharm using the same local PostgreSQL .env:
    python -m scripts.repair_appeal_helper_receipt a1a97880-ea24-4669-af90-f6e16c33f1eb
    python -m scripts.repair_appeal_helper_receipt a1a97880-ea24-4669-af90-f6e16c33f1eb --apply --confirm RECEIPT-ONLY

Dry-run is the default. Apply requires an explicit confirmation and passes
strict ownership, question-attempt, dated award and exactly-once checks.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from uuid import UUID

from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv())

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from persistence.database import engine  # noqa: E402
from persistence.models import (  # noqa: E402
    PlatinumHelperCall, PlatinumHelperPurchase, QuestionAppeal,
    WalletTransaction,
)

CONFIRM = "RECEIPT-ONLY"
HELPER_RECEIPT = {"tier": "gold", "amount": 1, "reason": "helper"}


def _utc(dt: datetime) -> datetime:
    return (dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None
            else dt.astimezone(timezone.utc))


def repair(db: Session, appeal_id: UUID, *, apply: bool = False) -> dict:
    """Verify a past payment and amend only verdict_data, never the wallet.

    A run-wide helper award is intentionally not reused for a second appeal:
    transaction timestamp must belong to this appeal's adjudication window.
    """
    appeal = db.get(QuestionAppeal, appeal_id, with_for_update=apply)
    if appeal is None:
        raise ValueError("No appeal found")
    if appeal.status != "upheld" or not appeal.closed_at:
        raise ValueError("Appeal was not upheld and settled")
    ctx = appeal.context or {}
    if not ctx.get("helper_used_on_question"):
        raise ValueError("Appeal did not snapshot a completed helper consultation")
    try:
        purchase_id = UUID(ctx["helper_purchase_id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("No valid helper purchase in frozen appeal context") from exc
    purchase = db.get(PlatinumHelperPurchase, purchase_id)
    if (purchase is None or purchase.run_id != appeal.run_id
            or purchase.user_id != appeal.user_id):
        raise ValueError("Helper purchase does not belong to the appealed run/user")
    if appeal.attempt_id is None:
        raise ValueError("Appealed attempt missing; cannot validate consultation")
    call = db.scalar(select(PlatinumHelperCall).where(
        PlatinumHelperCall.purchase_id == purchase.id,
        PlatinumHelperCall.question_attempt_id == appeal.attempt_id,
        PlatinumHelperCall.status == "completed",
        PlatinumHelperCall.created_at <= appeal.submitted_at,
    ).limit(1))
    if call is None:
        raise ValueError("No completed same-attempt helper consultation before filing")

    key = f"helper:{purchase.id}:appeal-gold"
    awards = db.scalars(select(WalletTransaction).where(
        WalletTransaction.idempotency_key == key,
        WalletTransaction.user_id == appeal.user_id,
        WalletTransaction.run_id == appeal.run_id,
    )).all()
    if (len(awards) != 1 or awards[0].amount != 1
            or awards[0].reason != "star/gold/helper-appeal-compensation"):
        raise ValueError("Missing or mismatched actual +1 gold transaction")
    award = awards[0]
    if (not (_utc(appeal.submitted_at) <= _utc(award.created_at)
             <= _utc(appeal.closed_at) + timedelta(minutes=5))):
        raise ValueError("Gold transaction belongs outside this appeal's settlement window")

    data = dict(appeal.verdict_data or {})
    receipts = list(data.get("expenses_refunded") or [])
    already = [x for x in receipts if x.get("reason") == "helper"]
    if already and already != [HELPER_RECEIPT]:
        raise ValueError("Conflicting helper receipt; refused to modify")
    if not already and apply:
        # New dict AND new list: required for SQLAlchemy JSON/JSONB tracking.
        data["expenses_refunded"] = [*receipts, dict(HELPER_RECEIPT)]
        appeal.verdict_data = data
        db.flush()
    return {
        "appeal_id": str(appeal_id),
        "status": "already_recorded" if already else
                  "repaired" if apply else "eligible_dry_run",
        "existing_refunds": receipts,
        "expected_missing_entry": None if already else HELPER_RECEIPT,
        "no_wallet_writes": True,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("appeal_id", type=UUID)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()
    if args.apply and args.confirm != CONFIRM:
        parser.error(f"--apply requires --confirm {CONFIRM}")
    with Session(engine()) as db:
        try:
            result = repair(db, args.appeal_id, apply=args.apply)
            if args.apply:
                db.commit()
            else:
                db.rollback()
            print(json.dumps(result, ensure_ascii=False, indent=2))
        except Exception:
            db.rollback()
            raise


if __name__ == "__main__":
    main()
