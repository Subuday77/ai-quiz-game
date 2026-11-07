"""Server-owned optional Q16 gold wager; existing star ledger is authoritative."""
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from persistence.gameplay import GameConflict, owned_game, pending_question
from persistence.models import GameRun, QuestionAttempt, WalletTransaction
from persistence.star_economy import lock_user_stars, star_balance


def accept_bonus(db: Session, *, user_id: UUID, run_id: UUID) -> GameRun:
    """Stake one earned gold star before Q16, once per game."""
    try:
        lock_user_stars(db, user_id)
        run = owned_game(db, run_id=run_id, user_id=user_id, for_update=True)
        if run.bonus_wager_accepted and run.status == "bonus":
            db.commit()
            return run
        if run.status != "bonus" or run.correct_main_answers != 15 or run.hints_used:
            raise GameConflict("Bonus wager not available")
        if pending_question(db, run_id=run_id) is not None:
            raise GameConflict("Bonus question already issued")
        if db.scalar(select(QuestionAttempt.id).where(
            QuestionAttempt.run_id == run_id, QuestionAttempt.number == 16
        )) is not None:
            raise GameConflict("Bonus round already started")
        if star_balance(db, user_id, "gold") < 1:
            raise GameConflict("A gold star is required for the bonus wager")
        db.add(WalletTransaction(
            user_id=user_id, run_id=run.id,
            idempotency_key=f"bonus-wager:{run.id}:stake",
            amount=-1, reason="star/gold/bonus-wager",
        ))
        run.bonus_wager_accepted = True
        db.commit()
        return run
    except Exception:
        db.rollback()
        raise


def decline_bonus(db: Session, *, user_id: UUID, run_id: UUID) -> GameRun:
    """Player declines Q16 and keeps earned gold."""
    try:
        run = owned_game(db, run_id=run_id, user_id=user_id, for_update=True)
        if run.status != "bonus" or run.correct_main_answers != 15:
            raise GameConflict("Bonus decision not available")
        if run.bonus_wager_accepted or pending_question(db, run_id=run_id) is not None:
            raise GameConflict("Bonus wager already committed")
        run.status = "completed"
        run.finished_at = datetime.now(timezone.utc)
        db.commit()
        return run
    except Exception:
        db.rollback()
        raise


def credit_won_gold(db: Session, *, run: GameRun) -> None:
    """Stage gold recovery in the same transaction as successful Q16 answer."""
    if run.bonus_wager_accepted:
        db.add(WalletTransaction(
            user_id=run.user_id, run_id=run.id,
            idempotency_key=f"bonus-wager:{run.id}:win",
            amount=1, reason="star/gold/bonus-win",
        ))
