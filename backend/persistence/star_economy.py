"""Server-side consumable star balances built from awards and a scoped ledger.

This is NOT billing or a purchasable-money wallet. Historical StarAward rows
remain immutable; the existing generic transaction table is namespaced with
'star/' reasons so earned and spendable counts remain distinct.

Every mutation must acquire the User row lock before reading balances and
must commit together with the protected effect (exchange/hint/skip).
"""
from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from persistence.models import StarAward, User, WalletTransaction
from star_perks import STAR_UNIT_VALUES, PerkUnavailable, StarTier, exchange_quote

STAR_TIERS = ("bronze", "silver", "gold", "platinum")


def lock_user_stars(db: Session, user_id: UUID) -> None:
    """Serialize every debit/credit for the player across concurrent games."""
    if db.scalar(select(User.id).where(User.id == user_id).with_for_update()) is None:
        raise PerkUnavailable("Unknown star owner")


def star_balance(db: Session, user_id: UUID, tier: StarTier) -> int:
    if tier not in STAR_TIERS:
        raise PerkUnavailable("Unknown star tier")
    earned = db.scalar(select(func.count(StarAward.id)).where(
        StarAward.user_id == user_id, StarAward.tier == tier
    )) or 0
    # Legacy wallet entries are unrelated and MUST NOT count as stars.
    debit_credit = db.scalar(select(func.coalesce(func.sum(WalletTransaction.amount), 0)).where(
        WalletTransaction.user_id == user_id,
        WalletTransaction.reason.like(f"star/{tier}/%"),
    )) or 0
    total = int(earned) + int(debit_credit)
    if total < 0:
        raise RuntimeError("Negative star balance — inconsistent ledger")
    return total


def star_balances(db: Session, user_id: UUID) -> dict[str, int]:
    return {tier: star_balance(db, user_id, tier) for tier in STAR_TIERS}


def spend_star(
    db: Session, *, user_id: UUID, run_id: UUID | None,
    tier: StarTier, amount: int, action: str, operation_key: str,
) -> None:
    """Stage a debit without committing; caller commits with game-state effect.

    The caller MUST hold the User lock and run lock if this is a game action.
    Operation keys are server derived; replay is rejected before writing.
    """
    if tier == "platinum" or tier not in STAR_UNIT_VALUES:
        raise PerkUnavailable("Platinum perks are not enabled")
    if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
        raise PerkUnavailable("Invalid star cost")
    if not action or len(action) > 30 or "/" in action:
        raise PerkUnavailable("Invalid star action")
    if not operation_key or len(operation_key) > 110:
        raise PerkUnavailable("Invalid operation key")
    if db.scalar(select(WalletTransaction.id).where(
        WalletTransaction.idempotency_key == operation_key
    )) is not None:
        raise PerkUnavailable("Action already applied")
    if star_balance(db, user_id, tier) < amount:
        raise PerkUnavailable("Insufficient stars")
    db.add(WalletTransaction(
        user_id=user_id, run_id=run_id,
        idempotency_key=operation_key, amount=-amount,
        reason=f"star/{tier}/{action}",
    ))
    # A pending debit is visible to this transaction after an autoflush.


def exchange_stars(
    db: Session, *, user_id: UUID, source: StarTier, target: StarTier,
    amount: int, request_id: UUID,
) -> dict[str, int]:
    """Exchange stars atomically, returning balances; safe to retry by UUID."""
    received = exchange_quote(source, target, amount)
    prefix = f"star:{user_id}:{request_id}"
    out_key, in_key = prefix + ":out", prefix + ":in"
    try:
        lock_user_stars(db, user_id)
        previous = db.scalar(select(WalletTransaction).where(
            WalletTransaction.idempotency_key == out_key
        ))
        if previous is not None:
            incoming = db.scalar(select(WalletTransaction).where(
                WalletTransaction.idempotency_key == in_key
            ))
            if not (previous.user_id == user_id and incoming is not None
                    and incoming.user_id == user_id
                    and previous.amount == -amount
                    and incoming.amount == received
                    and previous.reason == f"star/{source}/exchange-to-{target}"
                    and incoming.reason == f"star/{target}/exchange-from-{source}"):
                raise PerkUnavailable("Idempotency key reused for different exchange")
            result = star_balances(db, user_id)
            db.commit()
            return result
        if star_balance(db, user_id, source) < amount:
            raise PerkUnavailable("Insufficient stars")
        db.add_all([
            WalletTransaction(
                user_id=user_id, idempotency_key=out_key,
                amount=-amount, reason=f"star/{source}/exchange-to-{target}",
            ),
            WalletTransaction(
                user_id=user_id, idempotency_key=in_key,
                amount=received, reason=f"star/{target}/exchange-from-{source}",
            ),
        ])
        db.flush()  # Fail before commit if a duplicate operation is detected.
        result = star_balances(db, user_id)
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise
