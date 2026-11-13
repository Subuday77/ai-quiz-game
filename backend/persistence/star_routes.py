"""Authenticated star-balance and bidirectional conversion API.

No monetary payments, purchases, platinum redemption, complaints or ad logic.
"""
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from persistence.auth import authenticated_user, validate_origin
from persistence.about_bonus import about_bonus_claimed, claim_about_bonus
from persistence.database import get_db
from persistence.models import User
from persistence.star_economy import exchange_stars, star_balances
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
