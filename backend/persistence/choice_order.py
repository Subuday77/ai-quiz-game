"""Per-attempt option order. Never modify the shared Question's canonical keys.

choice_order encodes which canonical choice occupies display slots A/B/C/D.
Legacy NULL attempts remain identity so F5, historical appeals and answers do
not change after schema rollout. Each new issuance avoids the preceding order
for this question; all 24 permutations remain available over time.
"""
from __future__ import annotations

from collections.abc import Mapping
from itertools import permutations
from secrets import choice

from sqlalchemy import select
from sqlalchemy.orm import Session

from persistence.models import QuestionAttempt

LETTERS = "ABCD"
ORDERS = tuple("".join(p) for p in permutations(LETTERS))


def _order(value: str | None) -> str:
    order = value if value is not None else LETTERS
    if not isinstance(order, str) or len(order) != 4 or set(order) != set(LETTERS):
        raise ValueError("Invalid persisted option order")
    return order


def new_choice_order(previous: str | None = None) -> str:
    """Always reshuffle relative to the previous issuance (including legacy)."""
    last = _order(previous)
    return choice(tuple(order for order in ORDERS if order != last))


def last_choice_order(db: Session, question_id) -> str | None:
    latest = db.execute(
        select(QuestionAttempt.choice_order)
        .where(QuestionAttempt.question_id == question_id)
        .order_by(QuestionAttempt.shown_at.desc(), QuestionAttempt.id.desc())
        .limit(1)
    ).first()
    return latest[0] if latest is not None else None


def canonical_key(order: str | None, visible: str) -> str:
    if visible not in LETTERS or len(visible) != 1:
        raise ValueError("Invalid displayed option key")
    return _order(order)[LETTERS.index(visible)]


def display_key(order: str | None, canonical: str) -> str:
    if canonical not in LETTERS or len(canonical) != 1:
        raise ValueError("Invalid canonical option key")
    return LETTERS[_order(order).index(canonical)]


def display_choices(order: str | None, choices: dict) -> dict[str, str]:
    if set(choices) != set(LETTERS):
        raise ValueError("Invalid canonical option choices")
    return {letter: choices[source] for letter, source in zip(LETTERS, _order(order))}


def display_scores(order: str | None, scores: Mapping | None) -> dict | None:
    # Incomplete legacy scores must make Platinum abstain, not raise.
    if not isinstance(scores, Mapping) or any(key not in scores for key in LETTERS):
        return None
    return {letter: scores[source] for letter, source in
            zip(LETTERS, _order(order))}
