"""Delete expired 15-minute authentication rate-limit windows.

Run with the same DATABASE_URL as the Quiz backend:
python -m scripts.cleanup_auth_rate_buckets
"""
from datetime import datetime, timezone

from sqlalchemy import delete
from sqlalchemy.orm import Session

from persistence.account_rate_limit import WINDOW_SECONDS
from persistence.database import engine
from persistence.models import AuthRateBucket


def main():
    current = int(datetime.now(timezone.utc).timestamp() // WINDOW_SECONDS)
    cutoff = current - 2 * (86400 // WINDOW_SECONDS)
    with Session(engine()) as db:
        deleted = db.execute(delete(AuthRateBucket).where(
            AuthRateBucket.window_id < cutoff)).rowcount
        db.commit()
    print(f"auth_rate_buckets_pruned={deleted}")


if __name__ == "__main__":
    main()
