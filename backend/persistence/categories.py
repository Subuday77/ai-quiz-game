"""Per-game allowed categories and paid category operations.

Only categories in the game-owned allowlist can be issued, including on pool
fallback, topic retry and a new main-question revision. No model calls here.
"""
from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from consts import QUESTION_CATEGORIES
from persistence.gameplay import GameConflict, owned_game
from persistence.models import (
    Question, QuestionAttempt, Topic, WalletTransaction,
)
from persistence.seed_topics import category_slug
from persistence.star_economy import lock_user_stars, spend_star
from star_perks import (
    PerkUnavailable, category_change_price, validate_allowed_categories,
    EXCLUDE_CATEGORY_PRICE_SILVER,
)


def allowed_categories(run) -> tuple[str, ...]:
    allowed = run.allowed_categories
    if allowed is None:
        return tuple(QUESTION_CATEGORIES)
    allowed = tuple(allowed)
    if len(set(allowed)) != len(allowed) or not set(allowed).issubset(QUESTION_CATEGORIES):
        raise GameConflict("Invalid saved category allowlist")
    if len(allowed) < 16:
        raise GameConflict("Too few game categories")
    return allowed


def answered_category_slugs(db: Session, run_id: UUID) -> set[str]:
    """Only answered (not skipped, not superseded) categories are consumed."""
    return set(db.scalars(
        select(Topic.slug)
        .join(Question, Question.topic_id == Topic.id)
        .join(QuestionAttempt, QuestionAttempt.question_id == Question.id)
        .where(
            QuestionAttempt.run_id == run_id,
            QuestionAttempt.answered_at.is_not(None),
            QuestionAttempt.selected_key.is_not(None),
            QuestionAttempt.superseded_at.is_(None),
        )
    ))



def preceding_question_category(db: Session, run_id: UUID, number: int) -> str | None:
    """Last displayed, completed question of the immediately previous slot.

    Answered and paid-skipped questions both count; replaced revisions do not.
    Query persisted history rather than client state, including after restart.
    """
    if number <= 1:
        return None
    labels = db.scalar(
        select(Topic.labels)
        .join(Question, Question.topic_id == Topic.id)
        .join(QuestionAttempt, QuestionAttempt.question_id == Question.id)
        .where(
            QuestionAttempt.run_id == run_id,
            QuestionAttempt.number == number - 1,
            QuestionAttempt.answered_at.is_not(None),
            QuestionAttempt.superseded_at.is_(None),
        )
        .order_by(QuestionAttempt.revision.desc())
        .limit(1)
    )
    return labels.get("en") if isinstance(labels, dict) else None


def attempted_category_slugs(db: Session, run_id: UUID, number: int) -> set[str]:
    """All shown categories in the current slot, including superseded ones."""
    return set(db.scalars(
        select(Topic.slug)
        .join(Question, Question.topic_id == Topic.id)
        .join(QuestionAttempt, QuestionAttempt.question_id == Question.id)
        .where(QuestionAttempt.run_id == run_id, QuestionAttempt.number == number)
    ))


def available_categories(
    db: Session, run, number: int, *, changing: bool = False,
) -> list[str]:
    allowed = list(allowed_categories(run))
    consumed = answered_category_slugs(db, run.id)
    slot_history = attempted_category_slugs(db, run.id, number) if changing else set()
    items = [cat for cat in allowed if category_slug(cat) not in consumed
             and category_slug(cat) not in slot_history]
    if not items:
        # The running pool is exhausted; reset only from this game's allowlist.
        items = [cat for cat in allowed if category_slug(cat) not in slot_history]
    return items


def exclusion_price(db: Session, run_id: UUID, *, excluded: list[str]) -> int:
    if len(excluded) != len(set(excluded)) or any(
        item not in QUESTION_CATEGORIES for item in excluded
    ):
        raise PerkUnavailable("Invalid categories")
    remaining = validate_allowed_categories(QUESTION_CATEGORIES, excluded)
    return EXCLUDE_CATEGORY_PRICE_SILVER * (len(QUESTION_CATEGORIES) - len(remaining))


def exclude_pregame_categories(
    db: Session, *, run_id: UUID, user_id: UUID,
    categories: list[str], request_id: UUID,
) -> tuple[str, ...]:
    """Incrementally exclude paid categories before Q1, atomically."""
    operation_key = f"star:{user_id}:{request_id}:exclude"
    try:
        lock_user_stars(db, user_id)
        run = owned_game(db, run_id=run_id, user_id=user_id, for_update=True)
        previous = db.scalar(select(WalletTransaction).where(
            WalletTransaction.idempotency_key == operation_key))
        if previous is not None:
            if (previous.user_id != user_id or previous.run_id != run_id
                    or previous.reason != "star/silver/exclude-categories"
                    or previous.amount != -(5 * len(categories))):
                raise PerkUnavailable("Idempotency key reused")
            result = allowed_categories(run)
            db.commit()
            return result
        if run.status != "main":
            raise GameConflict("Categories can be excluded only before the main game")
        issued = db.scalar(select(func.count(QuestionAttempt.id)).where(
            QuestionAttempt.run_id == run_id))
        if issued:
            raise GameConflict("Cannot exclude categories after the first question")
        if not categories or len(categories) != len(set(categories)):
            raise PerkUnavailable("Choose distinct categories to exclude")
        old = allowed_categories(run)
        if not set(categories).issubset(old):
            raise PerkUnavailable("Category is not available for exclusion")
        remaining = validate_allowed_categories(old, categories)
        spend_star(
            db, user_id=user_id, run_id=run_id, tier="silver",
            amount=5 * len(categories), action="exclude-categories",
            operation_key=operation_key,
        )
        run.allowed_categories = list(remaining)
        db.commit()
        return remaining
    except Exception:
        db.rollback()
        raise


def category_snapshot(db: Session, run) -> dict:
    allowed = allowed_categories(run)
    attempts = db.scalars(select(QuestionAttempt).where(
        QuestionAttempt.run_id == run.id,
        QuestionAttempt.answered_at.is_(None),
        QuestionAttempt.superseded_at.is_(None),
    ).order_by(QuestionAttempt.number.desc())).all()
    active = attempts[0] if attempts else None
    if active is not None:
        current_category = db.scalar(
            select(Topic.labels)
            .join(Question, Question.topic_id == Topic.id)
            .where(Question.id == active.question_id)
        )
        current_category = current_category.get("en") if current_category else None
        possible = available_categories(db, run, active.number, changing=True)
        try:
            category_change_price(
                allowed_count=len(allowed),
                changes_used=active.category_change_count,
                manual_used=active.manual_category_used, manual=False)
        except PerkUnavailable:
            possible = []
        return dict(
            allowed=list(allowed), current=current_category,
            alternatives=possible, changes_used=active.category_change_count,
            manual_used=active.manual_category_used,
            can_change=bool(possible), can_choose=bool(possible)
            and active.manual_category_used < 1,
            can_exclude=False, minimum_allowed=16,
        )
    issued = db.scalar(select(func.count(QuestionAttempt.id)).where(
        QuestionAttempt.run_id == run.id)) or 0
    return dict(
        allowed=list(allowed), current=None, alternatives=[],
        changes_used=0, manual_used=0, can_change=False, can_choose=False,
        can_exclude=bool(run.status == "main" and not issued and len(allowed) > 16),
        minimum_allowed=16,
    )
