"""Authenticated star-balance and bidirectional conversion API.

No monetary payments, purchases, platinum redemption, complaints or ad logic.
"""
from uuid import UUID
from sqlalchemy import select

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from persistence.auth import authenticated_user, validate_origin
from persistence.about_bonus import about_bonus_claimed, claim_about_bonus
from persistence.database import get_db
from persistence.models import User, WalletTransaction
from persistence.star_economy import exchange_stars, star_balances, lock_user_stars, star_balance
from persistence.premium import premium_active
from star_perks import PerkUnavailable, StarTier

router = APIRouter(prefix="/api/v2/stars", tags=["Stars — consumable"])


class ExchangeInput(BaseModel):
    source: StarTier
    target: StarTier
    amount: int = Field(gt=0)
    request_id: UUID


class StarBalances(BaseModel):
    bronze: int
    silver: int
    gold: int
    platinum: int


@router.get("", response_model=StarBalances)
def balances(user: User = Depends(authenticated_user),
             db: Session = Depends(get_db)):
    return star_balances(db, user.id)


@router.get("/about-bonus")
def about_bonus_status(user: User = Depends(authenticated_user),
                       db: Session = Depends(get_db)):
    return {"claimed": about_bonus_claimed(db, user.id)}


@router.post("/about-bonus")
def about_bonus_claim(request: Request,
                      user: User = Depends(authenticated_user),
                      db: Session = Depends(get_db)):
    validate_origin(request)
    claimed_now, balances = claim_about_bonus(db, user.id)
    return {"claimed": True, "newly_claimed": claimed_now, "balances": balances}


@router.post("/exchange", response_model=StarBalances)
def exchange(payload: ExchangeInput, request: Request,
             user: User = Depends(authenticated_user),
             db: Session = Depends(get_db)):
    validate_origin(request)
    try:
        return exchange_stars(db, user_id=user.id,
                              source=payload.source, target=payload.target,
                              amount=payload.amount, request_id=payload.request_id)
    except PerkUnavailable as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


@router.post("/premium/gold-to-platinum", response_model=StarBalances)
def premium_gold_to_platinum(payload: ExchangeInput, request: Request,
                             user: User = Depends(authenticated_user),
                             db: Session = Depends(get_db)):
    """One-way Premium-only 10 gold → 1 platinum, ledger-atomic and retry-safe."""
    validate_origin(request)
    if payload.source != "gold" or payload.target != "platinum" or payload.amount != 10:
        raise HTTPException(status_code=422, detail="Only 10 gold to 1 platinum is supported")
    base = f"star:{user.id}:{payload.request_id}:premium"
    out_key, in_key = base + ":out", base + ":in"
    try:
        lock_user_stars(db, user.id)
        existing = db.scalar(select(WalletTransaction).where(
            WalletTransaction.idempotency_key == out_key))
        if existing:
            incoming = db.scalar(select(WalletTransaction).where(
                WalletTransaction.idempotency_key == in_key))
            if (existing.user_id != user.id or existing.amount != -10
                    or existing.reason != "star/gold/premium-to-platinum"
                    or incoming is None or incoming.user_id != user.id
                    or incoming.amount != 1
                    or incoming.reason != "star/platinum/premium-from-gold"):
                raise HTTPException(status_code=409, detail="Exchange request ID reused")
            result = star_balances(db, user.id)
            db.commit()
            return result
        if not premium_active(db, user.id):
            raise HTTPException(status_code=403, detail="Active Premium membership required")
        if star_balance(db, user.id, "gold") < 10:
            raise HTTPException(status_code=409, detail="Insufficient gold stars")
        db.add_all([
            WalletTransaction(user_id=user.id, amount=-10,
                              idempotency_key=out_key,
                              reason="star/gold/premium-to-platinum"),
            WalletTransaction(user_id=user.id, amount=1,
                              idempotency_key=in_key,
                              reason="star/platinum/premium-from-gold"),
        ])
        db.flush()
        result = star_balances(db, user.id)
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise
