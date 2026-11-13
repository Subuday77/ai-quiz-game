"""One-time About page reward, credited transactionally to the star ledger."""
from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from persistence.models import WalletTransaction
from persistence.star_economy import lock_user_stars, star_balances

ABOUT_BONUS_AMOUNTS = {
    "bronze": 50,
    "silver": 40,
    "gold": 30,
    "platinum": 20,
}
BONUS_VERSION = "about-read-v1"


def _key(user_id: UUID, tier: str) -> str:
    return f"star:{BONUS_VERSION}:{user_id}:{tier}"


def about_bonus_claimed(db: Session, user_id: UUID) -> bool:
    keys = [_key(user_id, tier) for tier in ABOUT_BONUS_AMOUNTS]
    rows = db.scalars(select(WalletTransaction).where(
        WalletTransaction.user_id == user_id,
        WalletTransaction.idempotency_key.in_(keys),
    )).all()
    if rows and len(rows) != len(ABOUT_BONUS_AMOUNTS):
        # A four-tier reward is strictly all-or-nothing.
        raise RuntimeError("Inconsistent one-time About bonus ledger")
    return len(rows) == len(ABOUT_BONUS_AMOUNTS)


def claim_about_bonus(db: Session, user_id: UUID) -> tuple[bool, dict[str, int]]:
    """Claim at most once, even when several tabs POST concurrently.

    A PostgreSQL user-row lock serializes the check and the four credits.
    Unique ledger operation keys enforce the invariant across restarts.
    """
    try:
        lock_user_stars(db, user_id)
        if about_bonus_claimed(db, user_id):
            balances = star_balances(db, user_id)
            db.commit()
            return False, balances
        db.add_all([
            WalletTransaction(
                user_id=user_id,
                idempotency_key=_key(user_id, tier),
                amount=amount,
                reason=f"star/{tier}/about-read-bonus",
            )
            for tier, amount in ABOUT_BONUS_AMOUNTS.items()
        ])
        db.flush()
        balances = star_balances(db, user_id)
        db.commit()
        return True, balances
    except Exception:
        db.rollback()
        raise
