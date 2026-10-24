"""Idempotently add existing quiz topics to the PostgreSQL topic catalog.

Run from backend/ after alembic upgrade head:
    python -m persistence.seed_topics

This is NOT topic tagging. Tags/keywords are to be created separately.
"""
import hashlib
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from consts import QUESTION_CATEGORIES
from persistence.database import engine
from persistence.models import Topic


def category_slug(name: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")
    return f"{text[:80]}-{hashlib.sha256(name.encode('utf-8')).hexdigest()[:8]}"


def seed_topics(db: Session) -> int:
    created = 0
    for title in QUESTION_CATEGORIES:
        slug = category_slug(title)
        if db.scalar(select(Topic.id).where(Topic.slug == slug)) is None:
            db.add(Topic(slug=slug, labels={"en": title}))
            created += 1
    db.commit()
    return created


def main():
    with Session(engine()) as db:
        created = seed_topics(db)
    print(f"Seeded {created} missing topics.")


if __name__ == "__main__":
    main()
