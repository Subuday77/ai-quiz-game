"""Signed Buy Me a Coffee membership ingest and account Premium policy.

Only real, signed membership events for the configured Premium tier can grant
benefits. Buyer email must match an already verified AI Quiz account. No donation
or unsupported membership level can grant stars or entitlements.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from persistence.account_email import canonical_email
from persistence.auth import authenticated_user
from persistence.database import get_db
from persistence.models import PremiumMembership, PremiumWebhookEvent, User

router = APIRouter(prefix="/api/v2/premium", tags=["Premium"])

MONTHLY_FREE_APPEALS = 8
DISCOUNT_PERCENT = 20
_MEMBERSHIP_EVENTS = {
    "membership.started", "membership.updated", "membership.cancelled",
    "membership.paused",
}


def premium_active(db: Session, user_id: UUID, *, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    membership_id = db.scalar(select(PremiumMembership.id).where(
        PremiumMembership.user_id == user_id,
        PremiumMembership.status.in_(("active", "cancelled")),
        PremiumMembership.current_period_end > now,
    ).limit(1))
    # A real database yields the typed UUID primary key. Do not treat mocked
    # scalar placeholder objects as evidence of a paid entitlement.
    return isinstance(membership_id, UUID)


def status(db: Session, user_id: UUID) -> dict:
    active = premium_active(db, user_id)
    return {
        "active": active,
        "checkout_ready": bool(os.environ.get("BMC_WEBHOOK_SIGNING_SECRET")
                               and os.environ.get("BMC_PREMIUM_LEVEL_ID", "").strip().isdecimal()),
        "free_appeals_per_month": MONTHLY_FREE_APPEALS if active else 5,
        "gold_to_platinum": active,
        "star_discount_percent": DISCOUNT_PERCENT if active else 0,
        "priority_questions": active,
    }


def discounted_price_cents(base_cents: int, *, active: bool) -> int:
    """Deterministic server-side quote helper for future star-pack checkout.

    No star purchases may be granted until a trusted payment receipt is verified.
    """
    if isinstance(base_cents, bool) or not isinstance(base_cents, int) or base_cents < 0:
        raise ValueError("Invalid base price")
    return (base_cents * (100 - DISCOUNT_PERCENT) + 50) // 100 if active else base_cents


@router.get("")
def get_premium(user: User = Depends(authenticated_user),
                db: Session = Depends(get_db)):
    return status(db, user.id)


@router.post("/webhook")
async def membership_webhook(request: Request, db: Session = Depends(get_db)):
    secret = os.environ.get("BMC_WEBHOOK_SIGNING_SECRET", "")
    level_id = os.environ.get("BMC_PREMIUM_LEVEL_ID", "").strip()
    if not secret or not level_id.isdecimal():
        raise HTTPException(status_code=503, detail="Premium webhook is not configured")
    raw = await request.body()
    if len(raw) > 65536:
        raise HTTPException(status_code=413, detail="Webhook too large")
    signature = request.headers.get("x-signature-sha256", "")
    expected = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
    if not re.fullmatch(r"[0-9a-fA-F]{64}", signature) or not hmac.compare_digest(
        expected, signature.lower()
    ):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="Invalid webhook JSON") from None
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid event")
    event_id = payload.get("event_id")
    event_type = payload.get("type")
    if isinstance(event_id, bool) or not isinstance(event_id, int) or event_id < 1:
        raise HTTPException(status_code=400, detail="Invalid event id")
    if not isinstance(event_type, str) or len(event_type) > 60:
        raise HTTPException(status_code=400, detail="Invalid event type")
    # Dashboard test deliveries reuse event_id=1; never let a test receipt
    # collide with a real charge bearing the same provider event ID.
    key = f"{'live' if payload.get('live_mode') is True else 'test'}:{event_id}"
    # Serialize deliveries by event ID; unlike a SELECT-before-INSERT this is
    # safe against simultaneous retries in separate API workers.
    if db.get(PremiumWebhookEvent, key) is not None:
        return {"accepted": True, "duplicate": True}
    from sqlalchemy.exc import IntegrityError
    try:
        with db.begin_nested():
            db.add(PremiumWebhookEvent(
                provider_event_id=key, event_type=event_type, outcome="ignored"
            ))
            db.flush()
    except IntegrityError:
        db.rollback()
        return {"accepted": True, "duplicate": True}

    data = payload.get("data")
    # Dashboard test events must never issue paid entitlements.
    if payload.get("live_mode") is not True or event_type not in _MEMBERSHIP_EVENTS:
        db.commit()
        return {"accepted": True, "applied": False}
    if not isinstance(data, dict) or str(data.get("membership_level_id")) != level_id:
        db.commit()
        return {"accepted": True, "applied": False}

    subscription_id = data.get("id")
    supporter_id = data.get("supporter_id")
    until = data.get("current_period_end")
    timestamp = payload.get("created")
    plan = data.get("duration_type")
    if (isinstance(subscription_id, bool) or not isinstance(subscription_id, int)
            or subscription_id < 1 or isinstance(supporter_id, bool)
            or not isinstance(supporter_id, int) or supporter_id < 1
            or isinstance(until, bool) or not isinstance(until, int)
            or isinstance(timestamp, bool) or not isinstance(timestamp, int)
            or plan not in ("month", "year")):
        raise HTTPException(status_code=422, detail="Incomplete membership event")
    # Bind by verified account mailbox on the initial event, then lock that
    # immutable user/supporter association for subsequent renewals.
    try:
        email = canonical_email(data["supporter_email"])
    except (KeyError, ValueError):
        raise HTTPException(status_code=422, detail="Membership email unavailable") from None
    subscription_key = str(subscription_id)
    row = db.scalar(select(PremiumMembership).where(
        PremiumMembership.provider == "buymeacoffee",
        PremiumMembership.provider_subscription_id == subscription_key,
    ).with_for_update())
    user = None
    if row is None:
        user = db.scalar(select(User).where(
            User.email == email,
            User.email_verified_at.is_not(None),
            User.disabled_at.is_(None),
        ).with_for_update())
        if user is None:
            # No free Premium for unmatched/unverified buyers; keep an audit
            # receipt, later manual reconciliation can be implemented safely.
            db.commit()
            return {"accepted": True, "applied": False}
    else:
        if row.provider_supporter_id != str(supporter_id):
            raise HTTPException(status_code=409, detail="Supporter identity mismatch")
        user = db.get(User, row.user_id)
        if user is None or user.email != email:
            raise HTTPException(status_code=409, detail="Subscription identity mismatch")

    occurred = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    period_end = datetime.fromtimestamp(until, tz=timezone.utc)
    if row is not None:
        last_created = (row.last_event_created if row.last_event_created.tzinfo
                        else row.last_event_created.replace(tzinfo=timezone.utc))
        if occurred < last_created or (
            occurred == last_created and event_id <= row.last_event_id
        ):
            db.commit()
            return {"accepted": True, "applied": False}
    cancelled = event_type == "membership.cancelled" or data.get("status") == "canceled"
    paused = event_type == "membership.paused" or data.get("status") == "paused"
    state = "paused" if paused else "cancelled" if cancelled else "active"
    if row is None:
        row = PremiumMembership(
            user_id=user.id, provider="buymeacoffee",
            provider_subscription_id=subscription_key,
            provider_supporter_id=str(supporter_id),
            plan=plan, status=state,
            current_period_end=period_end, last_event_created=occurred,
            last_event_id=event_id,
        )
        db.add(row)
    else:
        row.plan = plan
        row.status = state
        row.current_period_end = period_end
        row.last_event_created = occurred
        row.last_event_id = event_id
        row.updated_at = datetime.now(timezone.utc)
    receipt = db.get(PremiumWebhookEvent, key)
    receipt.outcome = "applied"
    db.commit()
    return {"accepted": True, "applied": True}
