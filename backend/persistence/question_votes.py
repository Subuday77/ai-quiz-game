"""Authenticated player vote policy. No payment, hints or answer-key exposure.

Unique (question_id, user_id) ballot and a row-locked aggregate ensure a
player can toggle or change their own vote without inflating the score.
The single persistent community-review row is a durable queue marker.
"""
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from persistence.gameplay import GameConflict, owned_game
from persistence.models import (
    Question, QuestionAttempt, QuestionVote, QuestionVoteReview, User,
)
from persistence.question_source_policy import community_vote_threshold


def my_vote(db: Session, *, user_id: UUID, question_id: UUID) -> int:
    return db.scalar(select(QuestionVote.value).where(
        QuestionVote.question_id == question_id,
        QuestionVote.user_id == user_id,
    )) or 0


def submit_vote(db: Session, *, run_id: UUID, question_id: UUID,
                user_id: UUID, value: int) -> tuple[int, bool]:
    """Save one ballot; return (my_vote, new_review_queued), never a tally."""
    if type(value) is not int or value not in (-1, 0, 1):
        raise ValueError("Vote must be -1, 0 or 1")
    threshold = community_vote_threshold()
    try:
        # Standard lock order: user -> run -> question.
        user_exists = db.scalar(select(User.id).where(
            User.id == user_id).with_for_update())
        if user_exists is None:
            raise GameConflict("User missing")
        run = owned_game(db, run_id=run_id, user_id=user_id, for_update=True)
        if run.status not in ("main", "bonus"):
            raise GameConflict("Game already ended")
        attempt = db.scalar(select(QuestionAttempt.id).where(
            QuestionAttempt.run_id == run.id,
            QuestionAttempt.question_id == question_id,
            QuestionAttempt.superseded_at.is_(None),
            QuestionAttempt.answered_at.is_(None),
        ).limit(1))
        if attempt is None:
            raise GameConflict("Vote requires the unanswered active question")
        question = db.scalar(select(Question).where(
            Question.id == question_id).with_for_update())
        if question is None or question.status != "approved":
            raise GameConflict("Question no longer available")
        vote = db.get(QuestionVote, (question_id, user_id))
        previous = vote.value if vote is not None else 0
        if value != previous:
            if value == 0:
                db.delete(vote)
            elif vote is None:
                db.add(QuestionVote(
                    question_id=question_id, user_id=user_id, value=value))
            else:
                vote.value = value
                vote.updated_at = datetime.now(timezone.utc)
            question.vote_score = int(question.vote_score or 0) + value - previous

        queued = False
        if question.vote_score <= threshold and db.get(
                QuestionVoteReview, question_id) is None:
            db.add(QuestionVoteReview(
                question_id=question_id,
                score_at_trigger=question.vote_score,
                status="pending",
            ))
            queued = True
        db.commit()
        return value, queued
    except Exception:
        db.rollback()
        raise
