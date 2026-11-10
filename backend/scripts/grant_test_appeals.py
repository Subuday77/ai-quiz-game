"""Grant additional free appeal filings to one test user for the UTC month.

Only for a deliberately opted-in local/test backend. This does NOT create
appeals, return deposits, rewrite complaint timestamps or alter the wallet.

Run from backend directory, after `alembic upgrade head`:

  python -m scripts.grant_test_appeals --username AI_Tester_1 --count 10
  python -m scripts.grant_test_appeals --username AI_Tester_1 --count 10 \
      --apply --confirm AI_Tester_1

The default invocation is a dry run. The app and script both require:
  QUIZ_QA_APPEAL_CREDITS_ENABLED=true

Disable that flag to return to the ordinary 5-free-per-month policy.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

from dotenv import find_dotenv, load_dotenv
from sqlalchemy import select
from sqlalchemy.orm import Session

load_dotenv(find_dotenv())

from persistence.appeal_policy import FREE_MONTHLY_APPEALS, month_bounds  # noqa: E402
from persistence.appeal_qa_quota import qa_credits_enabled  # noqa: E402
from persistence.appeal_service import _filings_this_month  # noqa: E402
from persistence.database import engine  # noqa: E402
from persistence.models import AppealQaFreeQuota, User  # noqa: E402
from persistence.star_economy import lock_user_stars  # noqa: E402


def grant(*, username: str, count: int, apply: bool = False,
          now: datetime | None = None) -> dict:
    if not qa_credits_enabled():
        raise RuntimeError("Set QUIZ_QA_APPEAL_CREDITS_ENABLED=true for local QA only")
    if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= 100:
        raise ValueError("Count must be between 1 and 100 additional free filings")
    if not username or len(username) > 30:
        raise ValueError("Explicit username required")
    moment = now or datetime.now(timezone.utc)
    start, end = month_bounds(moment)
    with Session(engine()) as db:
        user = db.scalar(select(User).where(User.username == username))
        if user is None:
            raise ValueError("Username not found in this database")
        if apply:
            # Same per-user row lock as appeal filing; no quote/fee race.
            lock_user_stars(db, user.id)
        row = db.get(AppealQaFreeQuota, (user.id, start), with_for_update=apply)
        previous = row.extra_free if row else 0
        if previous + count > 1000:
            raise ValueError("User's monthly QA grant would exceed 1000")
        submitted = _filings_this_month(db, user_id=user.id, now=moment)
        new_free_total = FREE_MONTHLY_APPEALS + previous + count
        result = {
            "mode": "APPLIED" if apply else "DRY_RUN",
            "username": username,
            "month_utc": start.strftime("%Y-%m"),
            "resets_at_utc": end.isoformat(),
            "submitted_this_month": submitted,
            "existing_qa_credits": previous,
            "grant_count": count,
            "new_qa_credits": previous + count,
            "free_total": new_free_total,
            "free_remaining": max(0, new_free_total - submitted),
        }
        if apply:
            if row is None:
                db.add(AppealQaFreeQuota(user_id=user.id, month_start=start,
                                         extra_free=previous + count))
            else:
                row.extra_free = previous + count
                row.updated_at = datetime.now(timezone.utc)
            db.commit()
        else:
            db.rollback()
        return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Grant account-scoped free monthly QA appeal filings (dry-run by default)")
    parser.add_argument("--username", required=True)
    parser.add_argument("--count", type=int, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()
    if args.apply and args.confirm != args.username:
        parser.error("For --apply provide --confirm with the exact --username")
    print(json.dumps(grant(username=args.username, count=args.count,
                           apply=args.apply), ensure_ascii=False, indent=2))
