"""Optional test-only additional free appeal filings.

An explicit local-only opt-in is required to use QA grants. The underlying
complaints, monthly counts, refunds and wallet history are never rewritten.
"""
from __future__ import annotations

import os
from datetime import datetime
from uuid import UUID

from sqlalchemy.orm import Session

from persistence.appeal_policy import FREE_MONTHLY_APPEALS, appeal_fee_bronze, month_bounds
from persistence.models import AppealQaFreeQuota


def qa_credits_enabled() -> bool:
    return os.getenv("QUIZ_QA_APPEAL_CREDITS_ENABLED", "false").strip().lower() == "true"


def extra_free_for_month(db: Session, *, user_id: UUID, now: datetime) -> int:
    if not qa_credits_enabled():
        return 0
    start, _ = month_bounds(now)
    row = db.get(AppealQaFreeQuota, (user_id, start))
    return int(row.extra_free) if row is not None else 0


def allowance(db: Session, *, user_id: UUID, now: datetime,
              submitted: int) -> dict:
    """Derived shared values for quote, UI allowance, and transactional filing.

    Free allowance expands from 5 to 5 + QA grants. Paid filing fees begin
    only after those additional credits are consumed. A separate star deposit
    is STILL required for every complaint.
    """
    if submitted < 0:
        raise ValueError("Monthly complaint count cannot be negative")
    grant = extra_free_for_month(db, user_id=user_id, now=now)
    _, refresh_at = month_bounds(now)
    total = FREE_MONTHLY_APPEALS + grant
    return {
        "free_appeals_remaining": max(0, total - submitted),
        "free_appeals_total": total,
        "free_appeals_refresh_at": refresh_at.isoformat(),
        "filing_fee_bronze": appeal_fee_bronze(max(0, submitted - grant)),
    }
