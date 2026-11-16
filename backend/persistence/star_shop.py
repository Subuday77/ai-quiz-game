"""Buy Me a Coffee Shop -> bronze stars, fail-closed until explicitly enabled.

Only signed, real, successful, exact-price orders for an allowlisted Shop extra
can add ledger credit. The browser never sends a star amount or payment receipt.
Original BMC payload, customer email and payment tokens are not persisted.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from persistence.account_email import canonical_email
from persistence.auth import authenticated_user
from persistence.database import get_db
from persistence.models import StarShopPurchase, StarShopWebhookEvent, User, WalletTransaction
from persistence.premium import discounted_price_cents, premium_active
from persistence.star_economy import lock_user_stars, star_balance

router = APIRouter(prefix="/api/v2/star-shop", tags=["Bronze star shop"])

# USD integer cents, immutable product catalog. No client-computed prices.
PACK_PRICES = {30: 300, 100: 900, 250: 2000, 500: 3500, 1000: 6000}
EVENTS = {"extra_purchase.created", "extra_purchase.refunded"}
_VALID_URL_HOSTS = {"buymeacoffee.com", "www.buymeacoffee.com"}


def _enabled() -> bool:
    return os.environ.get("BMC_STAR_SHOP_ENABLED", "").strip().lower() == "true"


def _configured_items() -> dict[int, dict]:
    """Return an all-or-nothing catalog with real, owner-configured Shop URLs.

    IDs are Buy Me a Coffee extras[].id. Never derive an ID or URL from the
    product name, amount, quantity or user-controlled input.
    """
    raw = os.environ.get("BMC_STAR_SHOP_ITEMS", "")
    try:
        obj = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    if not isinstance(obj, dict) or set(obj) != {str(n) for n in PACK_PRICES}:
        return {}
    result: dict[int, dict] = {}
    seen: set[int] = set()
    for count in PACK_PRICES:
        value = obj[str(count)]
        if not isinstance(value, dict) or set(value) != {"extra_id", "url"}:
            return {}
        extra_id = value["extra_id"]
        url = value["url"]
        if (type(extra_id) is not int or extra_id < 1 or extra_id in seen
                or not isinstance(url, str) or len(url) > 2048):
            return {}
        parsed = urlparse(url)
        if (parsed.scheme != "https" or parsed.hostname not in _VALID_URL_HOSTS
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or not parsed.path.startswith("/subuday/")):
            return {}
        seen.add(extra_id)
        result[count] = {"extra_id": extra_id, "url": url}
    return result


def _coupon() -> str | None:
    code = os.environ.get("BMC_STAR_PREMIUM_COUPON", "").strip()
    return code if re.fullmatch(r"[A-Za-z0-9_-]{4,32}", code) else None


def _money_cents(value: object) -> int | None:
    """BMC payment amounts are decimal USD, not integer cents."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        amount = Decimal(str(value))
        cents = amount * 100
        if not cents.is_finite() or cents != cents.to_integral_value():
            return None
        return int(cents) if 0 < cents <= 100000000 else None
    except (InvalidOperation, ValueError, OverflowError):
        return None


@router.get("")
def get_shop(user: User = Depends(authenticated_user)):
    catalog = _configured_items()
    live = _enabled() and bool(os.environ.get("BMC_STAR_WEBHOOK_SIGNING_SECRET")) and bool(catalog)
    verified = bool(user.email and user.email_verified_at and not user.disabled_at)
    premium = False  # Resolve from the same DB session below via status endpoint.
    # The authenticated_user dependency is a DB-bound User, but this GET uses a
    # separate DI Session only when explicitly declared; see get_shop below.
    return _shop_payload(user=user, catalog=catalog, live=live, verified=verified,
                         premium=premium)


def _shop_payload(*, user: User, catalog: dict, live: bool,
                  verified: bool, premium: bool) -> dict:
    coupon = _coupon() if premium else None
    can_checkout = live and verified and (not premium or bool(coupon))
    return {
        "ready": bool(can_checkout),
        "configured": bool(live),
        "verified_email": user.email if verified else None,
        "premium": premium,
        "premium_coupon": coupon,
        "packages": [{
            "bronze": bronze, "base_price_cents": price,
            "price_cents": discounted_price_cents(price, active=premium),
            "checkout_url": catalog[bronze]["url"] if can_checkout else None,
        } for bronze, price in PACK_PRICES.items()],
    }


@router.post("/webhook")
async def purchase_webhook(request: Request, db: Session = Depends(get_db)):
    secret = os.environ.get("BMC_STAR_WEBHOOK_SIGNING_SECRET", "")
    if not secret:
        raise HTTPException(status_code=503, detail="Shop webhook unavailable")
    raw = await request.body()
    if len(raw) > 65536:
        raise HTTPException(status_code=413, detail="Webhook too large")
    signature = request.headers.get("x-signature-sha256", "")
    expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    if not re.fullmatch(r"[a-fA-F0-9]{64}", signature) or not hmac.compare_digest(
        signature.lower(), expected
    ):
        raise HTTPException(status_code=401, detail="Invalid signature")
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="Invalid JSON") from None
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid event")
    event_id = payload.get("event_id")
    kind = payload.get("type")
    if type(event_id) is not int or event_id < 1 or not isinstance(kind, str):
        raise HTTPException(status_code=400, detail="Invalid event identifier")
    live_mode = payload.get("live_mode") is True
    key = f"{'live' if live_mode else 'test'}:{event_id}"
    if not live_mode or kind not in EVENTS:
        # Test deliveries and donation/membership/updates must never credit.
        return {"accepted": True, "applied": False}
    catalog = _configured_items()
    if not _enabled() or not catalog:
        # Do not acknowledge a real charge until the operator has explicitly
        # enabled the fully configured Shop. BMC will retry non-2xx deliveries.
        raise HTTPException(status_code=503, detail="Shop not configured")
    if db.get(StarShopWebhookEvent, key):
        return {"accepted": True, "duplicate": True}
    try:
        with db.begin_nested():
            receipt = StarShopWebhookEvent(
                provider_event_id=key, event_type=kind, outcome="needs_review"
            )
            db.add(receipt)
            db.flush()
    except IntegrityError:
        db.rollback()
        return {"accepted": True, "duplicate": True}
    data = payload.get("data")
    if not isinstance(data, dict) or type(data.get("id")) is not int or data["id"] < 1:
        db.rollback()
        raise HTTPException(status_code=422, detail="Incomplete shop payment")
    payment_id = str(data["id"])
    previous = db.scalar(select(StarShopPurchase).where(
        StarShopPurchase.provider_purchase_id == payment_id
    ).with_for_update())
    if kind == "extra_purchase.refunded":
        if previous is None:
            db.add(StarShopPurchase(
                provider_purchase_id=payment_id, status="refunded_before_credit",
                bronze_amount=0, price_cents=0, refunded_at=datetime.now(timezone.utc),
            ))
            receipt.outcome = "refunded_before_credit"
        elif previous.status == "credited":
            lock_user_stars(db, previous.user_id)
            if star_balance(db, previous.user_id, "bronze") >= previous.bronze_amount:
                db.add(WalletTransaction(
                    user_id=previous.user_id,
                    idempotency_key=f"star:shop:bmc:{payment_id}:refund",
                    amount=-previous.bronze_amount,
                    reason="star/bronze/shop-refund",
                ))
                previous.status = "refunded"
                receipt.outcome = "refunded"
            else:
                # Never make the wallet negative. This is an auditable manual
                # reconciliation case (stars were spent or exchanged already).
                previous.status = "refund_pending"
                receipt.outcome = "refund_pending"
            previous.refunded_at = datetime.now(timezone.utc)
        elif previous.status == "refunded_before_credit":
            receipt.outcome = "refunded_before_credit"
        elif previous.status in ("unmatched", "needs_review"):
            previous.status = "refunded"
            previous.refunded_at = datetime.now(timezone.utc)
            receipt.outcome = "refunded_no_credit"
        else:
            receipt.outcome = "duplicate_refund"
        db.commit()
        return {"accepted": True, "applied": receipt.outcome == "refunded"}

    # The immutable provider payment ID is the main exactly-once boundary;
    # event_id alone is insufficient if BMC issues multiple event envelopes.
    if previous is not None:
        receipt.outcome = "duplicate_purchase"
        db.commit()
        return {"accepted": True, "applied": False}

    eligible = (
        data.get("object") == "payment"
        and data.get("support_type") == "Extra"
        and data.get("status") == "succeeded"
        and data.get("refunded") in ("false", False)
        and data.get("currency") == "USD"
        and isinstance(data.get("extras"), list)
        and len(data["extras"]) == 1
    )
    item = data["extras"][0] if eligible else None
    bronze = None
    if isinstance(item, dict) and type(item.get("id")) is int:
        bronze = next((count for count, configured in catalog.items()
                       if configured["extra_id"] == item["id"]), None)
    if not (bronze and item.get("object") == "extra"
            and item.get("currency") == "USD" and type(item.get("quantity")) is int
            and item["quantity"] == 1):
        eligible = False
    paid = _money_cents(data.get("amount"))
    if paid is None:
        eligible = False
    try:
        email = canonical_email(data["supporter_email"])
    except (KeyError, ValueError, TypeError):
        email = None
    user = (db.scalar(select(User).where(
        User.email == email, User.email_verified_at.is_not(None),
        User.disabled_at.is_(None),
    ).with_for_update()) if email is not None else None)
    status = "needs_review"
    if eligible and user is None:
        status = "unmatched"
    elif eligible and user is not None:
        base_price = PACK_PRICES[bronze]
        discounted = discounted_price_cents(base_price, active=True)
        premium = premium_active(db, user.id)
        # Do not grant a discounted order to a non-Premium account.
        price_ok = paid == base_price or (
            premium and bool(_coupon()) and paid == discounted
        )
        if price_ok:
            lock_user_stars(db, user.id)
            db.add(WalletTransaction(
                user_id=user.id,
                idempotency_key=f"star:shop:bmc:{payment_id}:credit",
                amount=bronze, reason="star/bronze/shop-purchase",
            ))
            status = "credited"
    db.add(StarShopPurchase(
        provider_purchase_id=payment_id,
        user_id=user.id if user is not None else None,
        provider_extra_id=str(item["id"]) if isinstance(item, dict)
                          and type(item.get("id")) is int else None,
        bronze_amount=bronze or 0, price_cents=paid or 0, status=status,
    ))
    receipt.outcome = status
    db.commit()
    return {"accepted": True, "applied": status == "credited", "status": status}
