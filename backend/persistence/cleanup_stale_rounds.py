"""Explicit maintenance cleanup for old, unpaid unfinished quiz rounds.

Default is DRY RUN. Never removes GameRun rows, QuestionAttempts or wallet
history, and never archives a game with purchased or charged star perks.
From backend/: python -m persistence.cleanup_stale_rounds --username Alice
Review output, then add --apply. Stop the local FastAPI server while applying.
"""
import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from persistence.database import engine
from persistence.models import (
    GameRun, PlatinumHelperPurchase, QuestionAttempt, User, WalletTransaction,
)
from persistence.star_economy import lock_user_stars


def cleanup(db: Session, *, username: str, older_than_days: int, apply: bool) -> dict:
    if older_than_days < 1:
        raise ValueError("older_than_days must be at least 1")
    user = db.scalar(select(User).where(User.username_key == username.casefold()))
    if user is None:
        raise ValueError("Unknown username")
    if apply:
        lock_user_stars(db, user.id)
    cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
    old_runs = list(db.scalars(select(GameRun).where(
        GameRun.user_id == user.id,
        GameRun.status.in_(("main", "bonus")),
        GameRun.started_at < cutoff,
    ).order_by(GameRun.started_at, GameRun.id).with_for_update() if apply else
        select(GameRun).where(
            GameRun.user_id == user.id, GameRun.status.in_(("main", "bonus")),
            GameRun.started_at < cutoff,
        ).order_by(GameRun.started_at, GameRun.id)))
    result = {"archivable": [], "protected": [], "applied": apply}
    for run in old_runs:
        helper = db.scalar(select(PlatinumHelperPurchase.id).where(
            PlatinumHelperPurchase.run_id == run.id
        ).limit(1))
        charged = db.scalar(select(WalletTransaction.id).where(
            WalletTransaction.user_id == user.id,
            WalletTransaction.run_id == run.id,
            WalletTransaction.amount < 0,
            WalletTransaction.reason.like("star/%"),
        ).limit(1))
        issued = db.scalar(select(func.count(QuestionAttempt.id)).where(
            QuestionAttempt.run_id == run.id
        )) or 0
        detail = {
            "game_id": str(run.id), "started_at": str(run.started_at),
            "questions_issued": int(issued),
        }
        if helper is not None or charged is not None:
            result["protected"].append(detail)
        else:
            result["archivable"].append(detail)
            if apply:
                run.status = "abandoned"
                run.finished_at = datetime.now(timezone.utc)
    if apply:
        db.commit()
    return result


def load_backend_environment() -> None:
    """Load the same backend/.env used by FastAPI, without overriding shell ENV."""
    load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--username", required=True, help="Exact game account username")
    parser.add_argument("--older-than-days", type=int, default=1,
                        help="Only archive runs older than N days (min 1)")
    parser.add_argument("--apply", action="store_true",
                        help="Apply the update; without this flag only report")
    args = parser.parse_args()
    load_backend_environment()
    with Session(engine(), autoflush=False, expire_on_commit=False) as db:
        result = cleanup(db, username=args.username,
                         older_than_days=args.older_than_days, apply=args.apply)
    import json
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not args.apply:
        print("DRY RUN: no updates committed. Pass --apply to archive archivable runs.")


if __name__ == "__main__":
    main()
